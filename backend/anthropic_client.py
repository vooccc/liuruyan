"""
Anthropic provider client.
Supports streaming output, tool use, and heartbeat detection.
"""
import os
import json
import logging
import time
from typing import AsyncIterator, Awaitable, Callable, List, Dict, Any, Optional
from dataclasses import dataclass
from enum import StrEnum

logger = logging.getLogger(__name__)

try:
    from anthropic import AsyncAnthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

from .tools import get_tool_registry
from .runtime_budget import AggregateRuntimeBudget
from .runtime_schedule import build_budget_convergence_plan
from .tool_id_contract import ensure_tool_id
from .tool_loop_guard import (
    ToolLoopGuard,
    ToolLoopStop,
    build_tool_loop_notice_event,
    build_tool_loop_user_message,
)


class LLMErrorCode(StrEnum):
    RATE_LIMIT = "rate_limit"
    OVERLOADED = "overloaded"
    CONTEXT_TOO_LONG = "context_too_long"
    AUTHENTICATION = "authentication"
    NETWORK_ERROR = "network_error"
    RESPONSE_FORMAT = "response_format_error"
    UNKNOWN = "unknown"


@dataclass
class LLMError(Exception):
    """LLM 错误分类"""
    message: str
    code: LLMErrorCode
    retryable: bool = False
    retry_after_ms: int | None = None

    def __post_init__(self):
        super().__init__(self.message)

    def __str__(self) -> str:
        return self.message


def _is_anthropic_connect_error(exc: BaseException) -> bool:
    """识别底层网络/代理连接错误（区别于 5xx/429 这类 API 层错误）。

    覆盖：anthropic.APIConnectionError、httpx.ConnectError/ReadError/RemoteProtocolError、
    httpcore.ConnectError、socket OSError。这些都属于 "请求还没真正到 API" 或 "连接半路断了" 的
    场景，对幂等的 LLM 请求重试是安全的。
    """
    try:
        from anthropic import APIConnectionError as _AnthAPIConnectErr  # type: ignore
    except Exception:
        _AnthAPIConnectErr = ()  # type: ignore
    try:
        import httpx as _httpx  # type: ignore
        _httpx_errs = (
            _httpx.ConnectError,
            _httpx.ReadError,
            _httpx.WriteError,
            _httpx.RemoteProtocolError,
            _httpx.ConnectTimeout,
            _httpx.ReadTimeout,
            _httpx.ProxyError,
        )
    except Exception:
        _httpx_errs = ()  # type: ignore
    try:
        import httpcore as _httpcore  # type: ignore
        _httpcore_errs = (_httpcore.ConnectError, _httpcore.ReadError, _httpcore.WriteError)
    except Exception:
        _httpcore_errs = ()  # type: ignore

    if isinstance(exc, _AnthAPIConnectErr):  # type: ignore[arg-type]
        return True
    if _httpx_errs and isinstance(exc, _httpx_errs):
        return True
    if _httpcore_errs and isinstance(exc, _httpcore_errs):
        return True
    # 兜底：消息层面识别（SDK 把 "Connection error." 包成普通 Exception 的情况）
    msg = str(exc).lower()
    if "connection error" in msg or "connect error" in msg or "tls handshake" in msg:
        return True
    return False


def classify_anthropic_error(status_code: int, message: str) -> LLMError:
    """Classify Anthropic API errors."""
    lowered = (message or "").lower()
    if status_code == 429:
        return LLMError(message, LLMErrorCode.RATE_LIMIT, retryable=True, retry_after_ms=5000)
    if status_code in (529, 503):
        return LLMError(message, LLMErrorCode.OVERLOADED, retryable=True, retry_after_ms=5000)
    if status_code == 401:
        return LLMError(message, LLMErrorCode.AUTHENTICATION, retryable=False)
    if status_code == 403:
        if "subscription_not_found" in lowered or "no active subscription" in lowered:
            return LLMError("Anthropic 账户没有可用订阅，当前模型无法调用。请切换到 OpenAI 兼容 provider，或检查 Anthropic 账号订阅状态。", LLMErrorCode.AUTHENTICATION, retryable=False)
        return LLMError(message, LLMErrorCode.AUTHENTICATION, retryable=False)
    if status_code == 400 and "context" in lowered:
        return LLMError(message, LLMErrorCode.CONTEXT_TOO_LONG, retryable=False)
    return LLMError(message, LLMErrorCode.UNKNOWN, retryable=False)


def _split_system_and_chat_messages(
    messages: List[Dict[str, Any]],
) -> tuple[str, List[Dict[str, Any]]]:
    """从 messages 中剥离 system 消息，返回 (system_prompt, chat_messages)。

    Phase B/C 改造：兼容两种 content 形态。
    - str content：按原逻辑用 \\n\\n 拼接
    - list[ContentBlock] content（v2 路径）：合并所有 type=text 的 block

    非 system 消息直接透传（不解构 content），这样 SDK 拿到的就是原始结构：
    - v2=0：content 永远是 str（旧行为完全等价）
    - v2=1：content 可能是 list，Anthropic SDK 原生支持
    """
    system_parts: List[str] = []
    chat_messages: List[Dict[str, Any]] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            if isinstance(content, str):
                if content:
                    system_parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        txt = block.get("text", "")
                        if txt:
                            system_parts.append(txt)
            else:
                system_parts.append(str(content))
        else:
            chat_messages.append(m)

    system_prompt = "\n\n".join(system_parts) + ("\n\n" if system_parts else "")
    return system_prompt, chat_messages


def _normalize_tool_input(raw_input: Any, tool_name: str) -> Dict[str, Any]:
    """Convert tool input into a JSON object or raise a clear format error."""
    if raw_input is None or raw_input == "":
        return {}
    if isinstance(raw_input, dict):
        return raw_input
    if isinstance(raw_input, str):
        try:
            parsed = json.loads(raw_input)
        except json.JSONDecodeError as exc:
            preview = raw_input[:200].replace("\n", "\\n")
            raise LLMError(
                f"Tool input JSON parse failed for '{tool_name}': {exc}. Raw: {preview}",
                LLMErrorCode.RESPONSE_FORMAT,
                retryable=False,
            ) from exc
        if isinstance(parsed, dict):
            return parsed
        raise LLMError(
            f"Tool input for '{tool_name}' must be a JSON object, got {type(parsed).__name__}",
            LLMErrorCode.RESPONSE_FORMAT,
            retryable=False,
        )
    raise LLMError(
        f"Tool input for '{tool_name}' must be an object or JSON string, got {type(raw_input).__name__}",
        LLMErrorCode.RESPONSE_FORMAT,
        retryable=False,
    )


# Anthropic model presets.
ANTHROPIC_MODELS = [
    {"id": "claude-sonnet-4-6", "name": "Sonnet (1M context)", "icon": "🟠"},
    {"id": "claude-opus-4-6", "name": "Opus (1M context)", "icon": "🟣"},
    {"id": "claude-haiku-4-5", "name": "Haiku", "icon": "⚡"},
]


class AnthropicClient:
    """Anthropic native API client."""

    _auth_mode_cache: Dict[str, str] = {}

    def __init__(self, api_key: str = None, model: str = None, base_url: str = None, auth_mode: str = None, default_headers: Dict[str, str] = None):
        if not HAS_ANTHROPIC:
            raise ImportError("请安装 anthropic 包: pip install anthropic")

        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")
        self.model = model or os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
        # 对齐 Cherry：尾部版本号 /v1 /v2beta /v3alpha 等自动剥；# 后缀是 escape hatch。
        # 完整规则见 backend.utils.api_host。SDK 会在 base_url 后自动追加 /v1/messages。
        from backend.utils.api_host import normalize_anthropic_base_url
        self.base_url = normalize_anthropic_base_url(base_url)  # 可选，用于代理
        # default_headers：getrouter 等反代网关靠 User-Agent / HTTP-Referer / X-Title
        # 鉴别合法客户端，缺则返 "Your request was blocked" / 401。
        # 走 merge_default_llm_headers 注入项目级默认头（Cherry-style UA）+ caller override。
        # 注：anthropic-beta 不在这里注入，留给 _build_client() 按当前 self.model
        # 即时计算——支持 set_model 切换模型时重新嗅探 beta flag。
        from backend.utils.llm_headers import merge_default_llm_headers
        self.default_headers = dict(default_headers) if default_headers is not None else None
        self.timeout = int(os.getenv("ANTHROPIC_API_TIMEOUT_MS", "600000")) / 1000  # 默认 10 分钟，转为秒
        self.stream_idle_timeout = int(
            os.getenv("ANTHROPIC_STREAM_IDLE_TIMEOUT_MS")
            or os.getenv("LLM_STREAM_IDLE_TIMEOUT_MS")
            or "180000"
        ) / 1000
        cached_auth_mode = self._auth_mode_cache.get(self.base_url or "")
        self.auth_mode = (auth_mode or cached_auth_mode or os.getenv("ANTHROPIC_AUTH_MODE") or "api_key").lower()
        if self.auth_mode not in ("api_key", "auth_token"):
            self.auth_mode = "api_key"

        if not self.api_key:
            raise ValueError("请设置 ANTHROPIC_API_KEY 环境变量或传入 api_key 参数")

        self.client = self._build_client()
        self.tool_registry = get_tool_registry()

    def _build_client(self):
        client_kwargs: Dict[str, Any] = {"timeout": self.timeout}
        if self.auth_mode == "auth_token":
            client_kwargs["auth_token"] = self.api_key
        else:
            client_kwargs["api_key"] = self.api_key
        if self.base_url:
            # 注意：Anthropic SDK 会在 base_url 后自动追加 /v1/messages
            # 所以如果用户提供了 /anthropic，SDK 会请求 /anthropic/v1/messages
            client_kwargs["base_url"] = self.base_url
        # 拼最终 headers：base default_headers + 按 self.model 嗅探的 anthropic-beta。
        # 每次 _build_client() 都重算，保证 set_model 切换模型后 beta 也跟着切。
        # caller 在 self.default_headers 显式设了 anthropic-beta 时 merge 函数会尊重不覆盖。
        from backend.utils.anthropic_beta import merge_anthropic_beta_into_headers
        from backend.utils.llm_headers import merge_default_llm_headers
        headers = merge_default_llm_headers(self.default_headers, transport="anthropic_messages")
        merge_anthropic_beta_into_headers(headers, self.model)
        if headers:
            client_kwargs["default_headers"] = headers
        return AsyncAnthropic(**client_kwargs)

    def _switch_auth_mode(self, auth_mode: str):
        self.auth_mode = auth_mode
        if self.base_url:
            self._auth_mode_cache[self.base_url] = auth_mode
        self.client = self._build_client()

    def _should_retry_with_auth_token(self, exc: Exception) -> bool:
        if self.auth_mode == "auth_token" or not self.base_url or not self.api_key:
            return False
        if getattr(exc, "status_code", None) != 401:
            return False
        error_text = str(exc).lower()
        return "apikey not found" in error_text or "hmac signature cannot be verified" in error_text

    def _get_api_client(self):
        """拿 ApiClient（auth_token 模式直构 bypass cache，api_key 模式走 registry LRU）。

        共用给 _chat_one_round / _chat_stream / _chat_complete 三个调用点。

        cache key 不区分 auth_mode：同 (provider, model, key, base_url) 在
        api_key 与 auth_token 模式之间共用 cache 会让 _switch_auth_mode 后
        下次 get_client 命中老 api_key 模式实例继续 401 死锁。所以 auth_token
        模式直构 AnthropicApiClient(auth_token=...) 跨过 cache。

        Returns:
            ApiClient 实例（满足 ApiClient Protocol）
        Raises:
            LLMError: registry provider lookup 失败（fail-fast）
        """
        if self.auth_mode == "auth_token":
            from liuruyan.llm.clients.anthropic import AnthropicApiClient
            return AnthropicApiClient(
                api_key="",
                model=self.model,
                base_url=self.base_url,
                auth_token=self.api_key,
                timeout=self.timeout,
                default_headers=self.default_headers,
            )
        from liuruyan.llm import (
            ProviderNotFoundError,
            get_default_registry,
        )
        try:
            return get_default_registry().get_client(
                "anthropic",
                self.model,
                api_key=self.api_key,
                base_url_override=self.base_url,
                default_headers=self.default_headers,
            )
        except ProviderNotFoundError as exc:
            raise LLMError(
                f"Unknown provider 'anthropic': {exc}",
                LLMErrorCode.RESPONSE_FORMAT,
                retryable=False,
            ) from exc

    def set_model(self, model: str, base_url: str = None, api_key: str = None, auth_mode: str = None, default_headers: Dict[str, str] = None):
        """切换模型。"""
        self.model = model
        if api_key is not None:
            self.api_key = api_key
        if base_url is not None:
            from backend.utils.api_host import normalize_anthropic_base_url
            self.base_url = normalize_anthropic_base_url(base_url)
        # default_headers 语义同 set_model 其他参数：None = 保留旧值；
        # {} / 非空 dict = 显式赋值。这里汽车在 "探测周期" 传 None 不会清除。
        if default_headers is not None:
            from backend.utils.llm_headers import merge_default_llm_headers
            self.default_headers = dict(default_headers) if default_headers else None
        cached_auth_mode = self._auth_mode_cache.get(self.base_url or "")
        self.auth_mode = (auth_mode or cached_auth_mode or os.getenv("ANTHROPIC_AUTH_MODE") or "api_key").lower()
        if self.auth_mode not in ("api_key", "auth_token"):
            self.auth_mode = "api_key"
        self.client = self._build_client()

    async def chat(
        self,
        messages: List[Dict[str, str]],
        stream: bool = True,
        use_tools: bool = True,
        agent_id: str = "default",
        session_id: Optional[str] = None,
        prompt_cache_info: Dict = None,
        chain_hook: Optional[Callable[[Dict[str, Any]], Awaitable[bool]]] = None,
        allowed_tools_override: Optional[List[str]] = None,
    ) -> AsyncIterator[Dict]:
        """发送聊天请求

        Args:
            prompt_cache_info: Prompt caching 配置信息，包含:
                - enable_caching: 是否启用缓存
                - static: 静态部分（可缓存）
                - dynamic: 动态部分（不缓存）
            chain_hook: 可选，v2 chain runner 反向注入的 after_model 钩子；
                每轮 LLM 完成后被调，返 True 时立即终止工具循环。详见
                _chat_with_tools 的 chain_hook 参数说明。
        """
        # 2026-05-12 P3 方案：标记本次 LLM 调用走 Anthropic 原生路径，PDF document
        # block 可以 base64 直传给 Claude（file_tools._read_pdf 读这个 var 决定形态）。
        # ContextVar.set 在当前 asyncio task context 内生效，不污染并发的 OpenAI 路径。
        from backend.llm_client import _provider_supports_pdf_native_var
        _provider_supports_pdf_native_var.set(True)

        if use_tools:
            async for event in self._chat_with_tools(
                messages, stream,
                agent_id=agent_id, session_id=session_id,
                prompt_cache_info=prompt_cache_info,
                chain_hook=chain_hook,
                allowed_tools_override=allowed_tools_override,
            ):
                yield event
        else:
            if stream:
                async for event in self._chat_stream(messages, prompt_cache_info=prompt_cache_info):
                    yield event
            else:
                result = await self._chat_complete(messages, prompt_cache_info=prompt_cache_info)
                yield {"type": "content", "text": result}

    async def _chat_stream(
        self,
        messages: List[Dict[str, str]],
        prompt_cache_info: Dict = None,
        allow_auth_retry: bool = True,
    ) -> AsyncIterator[Dict]:
        """流式输出（无工具）。

        v2 A:迁移：内部从直调 self.client.messages.stream 切到 ApiClient.send_stream。
        thinking_kwargs / system cache_control 由 plugin 在 ApiClient.send_stream
        时自动注入（AnthropicThinkingPlugin / AnthropicCachePlugin），caller
        只透传 prompt_cache_info。
        """
        # 分离 system 消息（兼容 str / list content，详见 _split_system_and_chat_messages）
        system_prompt, chat_messages = _split_system_and_chat_messages(messages)

        api_client = self._get_api_client()
        from liuruyan.llm import CoreRequest, RequestContext
        from .runtime_budget import get_effective_max_output_tokens, get_effective_stream_idle_timeout

        req_extras: Dict[str, Any] = {}
        if prompt_cache_info:
            req_extras["prompt_cache_info"] = prompt_cache_info

        # 2026-05-25：辅助任务（思考块翻译等）关 reasoning。
        # 显式塞 temperature=0 → 触发 AnthropicThinkingPlugin 的 "caller 已塞
        # temperature 时尊重不覆盖"逃生舱（@anthropic_thinking.py:67），跳过
        # 自动注入 thinking={enabled, ...}。temperature=0 + non-thinking 是
        # Anthropic SDK 唯一支持的"不开 thinking"的合法组合（thinking 字段
        # 不接受 {"type": "disabled"}）。
        from backend.llm_client import _disable_thinking_var
        if _disable_thinking_var.get():
            req_extras["temperature"] = 0.0

        core_req = CoreRequest(
            messages=chat_messages,
            system=system_prompt,
            tools=None,
            model=self.model,
            max_tokens=get_effective_max_output_tokens("anthropic"),
            extras=req_extras,
        )
        core_ctx = RequestContext(cancel_event=None)

        # chunk-level idle timeout —— 必须用 wait_for 包 __anext__，不能在
        # async for 循环体内打 last_heartbeat 时间差判断（那种写法在上游静默
        # 挂死时无法触发，详见 backend/llm_stream_utils.py 文件头注释）。
        # P3-stream-timeout-cfg：从 runtime_budget.json 读。
        from .llm_stream_utils import iter_stream_with_idle_timeout
        timeout = get_effective_stream_idle_timeout("anthropic")

        try:
            async for ev in iter_stream_with_idle_timeout(
                api_client.send_stream(core_req, core_ctx),
                timeout,
                label="anthropic",
            ):
                if ev.type == "content_delta":
                    yield {"type": "content", "text": ev.text}
                elif ev.type == "reasoning_delta":
                    yield {"type": "thinking", "text": ev.text}
                # final_assistant_content / usage / round_end / error 等其他
                # 事件类型本路径不消费（无工具调用 / 无 token 算账需求）

        except Exception as e:
            if allow_auth_retry and self._should_retry_with_auth_token(e):
                self._switch_auth_mode("auth_token")
                async for event in self._chat_stream(
                    messages,
                    prompt_cache_info=prompt_cache_info,
                    allow_auth_retry=False,
                ):
                    yield event
                return
            if isinstance(e, LLMError):
                raise
            if isinstance(e, json.JSONDecodeError):
                raise LLMError(
                    f"Anthropic response JSON parse failed: {e}",
                    LLMErrorCode.RESPONSE_FORMAT,
                    retryable=False,
                ) from e
            # 分类错误
            if hasattr(e, "status_code"):
                raise classify_anthropic_error(e.status_code, str(e))
            raise LLMError(str(e), LLMErrorCode.NETWORK_ERROR, retryable=True)

    async def _chat_complete(
        self,
        messages: List[Dict[str, str]],
        prompt_cache_info: Dict = None,
        allow_auth_retry: bool = True,
    ) -> str:
        """非流式输出（累积 _chat_stream 的所有 content delta 拼成完整 str）。

        v2 A 迁移：原版用 messages.create(stream=False) 拿单一 Message，
        现版用 _chat_stream 累积。两条路径共用 ApiClient + plugin 链 → 等价
        且少一个 SDK 调用形态。auth retry 由 _chat_stream 内部处理。
        """
        parts: List[str] = []
        async for event in self._chat_stream(
            messages,
            prompt_cache_info=prompt_cache_info,
            allow_auth_retry=allow_auth_retry,
        ):
            if event.get("type") == "content":
                parts.append(event.get("text", ""))
        return "".join(parts)

    async def _finalize_after_tool_loop(
        self,
        system_prompt: str,
        chat_messages: List[Dict[str, Any]],
        stop: ToolLoopStop,
    ) -> str:
        final_messages: List[Dict[str, Any]] = []
        if system_prompt.strip():
            final_messages.append({"role": "system", "content": system_prompt.strip()})
        final_messages.extend(chat_messages)
        final_messages.append({
            "role": "user",
            "content": build_tool_loop_user_message(stop),
        })
        return await self._chat_complete(final_messages)

    async def _record_session_token_usage(self, session_id: Optional[str], final_message) -> None:
        """从 Anthropic final_message.usage 提取 token 数 → SessionManager.record_token_usage。

        失败静默吞掉（log 都不打），避免污染 LLM 调用主流程。
        """
        if not session_id or not final_message:
            return
        usage = getattr(final_message, "usage", None)
        if not usage:
            return
        try:
            from .session import get_session_manager
            await get_session_manager().record_token_usage(
                session_id,
                input_tokens=getattr(usage, "input_tokens", 0) or 0,
                output_tokens=getattr(usage, "output_tokens", 0) or 0,
                cache_read=getattr(usage, "cache_read_input_tokens", 0) or 0,
                cache_create=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            )
        except Exception:
            pass

    @staticmethod
    def _extract_round_token_usage(final_message) -> dict | None:
        """从 Anthropic final_message.usage 提取本轮 token 增量给 mw 10 用。

        跟 _record_session_token_usage 用同一份 usage 对象（保持一致），但只
        提取数据返出来不调 SessionManager。失败返 None（mw 10 跳过 emit）。
        """
        if not final_message:
            return None
        usage = getattr(final_message, "usage", None)
        if not usage:
            return None
        try:
            return {
                "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
                "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
                "cache_read": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
                "cache_create": int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
            }
        except Exception:
            return None

    async def _chat_one_round(
        self,
        chat_messages: List[Dict[str, Any]],
        system_param,
        anthropic_tools,
        *,
        agent_id: str,
        session_id: Optional[str],
        stream: bool,
        round_num: int = 0,
        api_client: Optional[Any] = None,
        prompt_cache_info: Optional[Dict] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """v2 C2-a / P1-E-2：单轮 Anthropic LLM stream + 工具 dispatch（async generator）。

        v2 P1-E-2：内部从直调 SDK + 手装 parser 切到 ApiClient.send_stream；
        ApiClient 实例由 ProviderRegistry 按 (provider, model, key, base_url)
        LRU 缓存。auth_token 模式直构 bypass cache。可选 ``api_client`` 注入参数
        用于测试 / future LeadAgent 接管循环。

        从 _chat_with_tools 抽出来作为公开 API，让 v2 chain（LeadAgent）将来能够
        自己跑工具循环时调本方法做"一轮"——chain 接管循环外的 budget/guard/spawn
        决策逻辑，本方法只管"跑一轮"。

        yield: content / thinking / tool_call / tool_result / system_notice 事件透传给调用方。
        最后 yield 一个 type='_round_done' 内部事件携带 round 状态：
            - finish_reason: str | None
            - has_tool_calls: bool
            - assistant_msg: dict | None    （调用方 append 到 chat_messages）
            - tool_result_msg: dict | None
            - executed_tool_calls: list[dict]
            - executed_tool_results: list[str]
            - spawn_dispatched: bool

        ``_round_done`` 是内部约定（前缀下划线）— 调用方据此决策；不该泄露给
        外部 chat() 调用方。

        异常**不**捕获（auth retry / classify error 由外层 _chat_with_tools 接住）。
        例外：registry provider lookup 失败时直接抛 LLMError（fail-fast，不让
        fallback 误认 SDK 错误）。

        chat_messages 不在此 in-place 修改；通过 _round_done 事件返出 assistant_msg
        和 tool_result_msg，调用方决定何时 append。

        参数：
        - chat_messages: 当前对话上下文（不含 system，已分离）
        - system_param: prepare_system_with_cache 输出（str 或 list[block]
          带 cache_control 结构化形式）
        - anthropic_tools: get_tool_definitions(allowed) 输出
        - agent_id / session_id: 透传给 dispatch_batch + token usage 记账
        - stream: 是否 yield 流事件给调用方
        - round_num: 仅日志用
        - api_client: 可注入的 ApiClient 实例（测试 / 自定义路由用）；None 时
          按 self.auth_mode 走 registry 或直构。
        """
        import asyncio as _asyncio

        # P1-E-2 / A 迁移：用 _get_api_client() helper 共用三个调用点
        # （_chat_one_round / _chat_stream / _chat_complete）
        if api_client is None:
            api_client = self._get_api_client()

        from liuruyan.llm import CoreRequest, RequestContext

        # CoreRequest 等价性映射（vs 原 SDK 入参）：
        # - messages = chat_messages 直传
        # - system_param 是 plain str 或 None（P2-C 起 caller 不再传 list[block]，
        #   prompt cache 由 AnthropicCachePlugin 接管：把 prompt_cache_info 塞
        #   extras → plugin 在 ApiClient.send_stream 时改 req.system / extras["system"])
        # - tools = anthropic_tools 直传（已是 Anthropic 格式 input_schema）
        # - max_tokens = 8192 显式（原硬编码）
        # - thinking_kwargs / temperature 由 AnthropicThinkingPlugin 自动按 model 注入
        # - prompt_cache_info（caller 透传）→ extras["prompt_cache_info"]，
        #   AnthropicCachePlugin 消费后转 list[block] cache_control 结构
        req_extras: Dict[str, Any] = {}
        if prompt_cache_info:
            req_extras["prompt_cache_info"] = prompt_cache_info

        # P3-max-tokens-cfg：从 runtime_budget.json 读 max_output_tokens，让用户在
        # "预算"对话框里能调。老默认 8192 向后兼容（未配置时走 env → 默认）。
        from .runtime_budget import get_effective_max_output_tokens, get_effective_stream_idle_timeout
        core_req = CoreRequest(
            messages=chat_messages,
            system=system_param,
            tools=anthropic_tools,
            model=self.model,
            max_tokens=get_effective_max_output_tokens("anthropic"),
            extras=req_extras,
        )
        core_ctx = RequestContext(
            agent_id=agent_id,
            session_id=session_id,
            cancel_event=None,
        )

        # chunk-level idle timeout helper：详见 backend/llm_stream_utils.py
        # P3-stream-timeout-cfg：从 runtime_budget.json 读。
        from .llm_stream_utils import iter_stream_with_idle_timeout
        timeout = get_effective_stream_idle_timeout("anthropic")

        # 内层重试：只对未 yield 过有用事件的网络错误重试（round_yielded_useful 锁）
        # 2026-05-21 上限 3→2：外层 llm_client._chat_with_retry 之前也会把
        # NETWORK_ERROR 当 retryable 再开 1+2=3 轮，最坏 3×3=9 次。同日已在外层
        # 短路 NETWORK_ERROR（@backend/llm_client.py:_is_retryable_upstream_error），
        # 现在网络错只由本层负责，2 次足够覆盖瞬时抖动。
        inner_attempt = 0
        inner_max_retries = 2
        current_content = ""
        current_thinking = ""
        current_tool_calls: Dict[int, Dict] = {}
        finish_reason = None
        final_message = None
        while True:
            current_content = ""
            current_thinking = ""
            current_tool_calls = {}
            finish_reason = None
            final_message = None
            round_yielded_useful = False
            try:
                async for ev in iter_stream_with_idle_timeout(
                    api_client.send_stream(core_req, core_ctx),
                    timeout,
                    label="anthropic_tools",
                ):
                    ev_type = ev.type
                    if ev_type == "content_delta":
                        current_content += ev.text
                        if stream:
                            yield {"type": "content", "text": ev.text}
                            round_yielded_useful = True
                    elif ev_type == "reasoning_delta":
                        current_thinking += ev.text
                        if stream:
                            yield {"type": "thinking", "text": ev.text}
                            round_yielded_useful = True
                    elif ev_type == "tool_call_delta":
                        idx = ev.index
                        if idx not in current_tool_calls:
                            # 首次出现该 index：tool_use content_block_start
                            # 翻译过来的 ev 含 id+name；input_json_delta 翻译过来
                            # 的 ev 仅含 arguments，不会走这条分支
                            # P0 契约：tool_use_id 非空非 None；上游（反代 /
                            # 第三方 Anthropic 兼容协议）漏 id 时 synthesize
                            # 而非用 f"tool_{idx}"——后者会让多个空 id 碰撞
                            # 到相同占位，配对失真触发 Anthropic 400
                            current_tool_calls[idx] = {
                                "id": ensure_tool_id(ev.id, origin="anthropic_client_stream_toolcall"),
                                "name": ev.name or "unknown",
                                "input": ""
                            }
                            # P1 体感优化（2026-05-16）：name 已就位（Anthropic
                            # content_block_start 直接带 name），立即 yield 一个
                            # tool_call_started 让前端先渲染 calling 卡。后续
                            # 完整 tool_call（line ~830）按 tool_use_id 覆盖更新。
                            if stream and ev.name:
                                yield {
                                    "type": "tool_call_started",
                                    "name": ev.name,
                                    "tool_use_id": current_tool_calls[idx]["id"],
                                    "id": current_tool_calls[idx]["id"],
                                }
                            round_yielded_useful = True
                        else:
                            if ev.id:
                                current_tool_calls[idx]["id"] = ev.id
                            if ev.name:
                                current_tool_calls[idx]["name"] = ev.name
                        if ev.arguments:
                            current_tool_calls[idx]["input"] += ev.arguments
                    elif ev_type == "final_assistant_content":
                        final_message = ev.final_message
                    elif ev_type == "usage":
                        # Anthropic usage 已在 final_message 里，本事件冗余跳过
                        pass
                    elif ev_type == "round_end":
                        finish_reason = ev.finish_reason
                    elif ev_type == "error":
                        logger.error("[anthropic_client] stream parser error: %s", ev.message)
                break
            except Exception as inner_e:
                is_network_retryable = (
                    _is_anthropic_connect_error(inner_e)
                    or (
                        isinstance(inner_e, LLMError)
                        and inner_e.code == LLMErrorCode.NETWORK_ERROR
                        and inner_e.retryable
                    )
                )
                if (
                    not round_yielded_useful
                    and inner_attempt < inner_max_retries
                    and is_network_retryable
                ):
                    inner_attempt += 1
                    delay = min(2 ** inner_attempt, 8)
                    logger.warning(
                        "[anthropic_client] round %s stream connect failed (%s: %s); retry %s/%s in %ss",
                        round_num, type(inner_e).__name__, inner_e, inner_attempt, inner_max_retries, delay,
                    )
                    yield {
                        "type": "system_notice",
                        "kind": "network_retry",
                        "content": (
                            f"上游连接异常（{type(inner_e).__name__}），"
                            f"第 {inner_attempt}/{inner_max_retries} 次重试，{delay}s 后重连…"
                        ),
                    }
                    await _asyncio.sleep(delay)
                    last_heartbeat = time.time()
                    continue
                raise

        # token usage 算账（per-session 累计）— 失败不影响 LLM 主流程
        await self._record_session_token_usage(session_id, final_message)

        # mw 10 TokenUsage chain 镜像：把本轮 usage 增量提取出来挂在 _round_done
        # payload，让 LeadAgent chain_hook 写 state["last_round_token_usage"]，
        # mw 10 在 after_model emit token_usage_recorded 事件做 trace journal 标记
        # （v1 仍真算账写 per-session usage.json + token_usage_updated；mw 10
        # 是观察者形态不重复算账）
        round_token_usage = self._extract_round_token_usage(final_message)

        finish_reason = finish_reason or getattr(final_message, "stop_reason", None)
        has_tool_calls = (
            finish_reason == "tool_use"
            and (current_tool_calls or final_message is not None)
        )

        if not has_tool_calls:
            yield {
                "type": "_round_done",
                "finish_reason": finish_reason,
                "has_tool_calls": False,
                "assistant_msg": None,
                "tool_result_msg": None,
                "executed_tool_calls": [],
                "executed_tool_results": [],
                "spawn_dispatched": False,
                "token_usage": round_token_usage,
            }
            return

        # ── 组装 finalized_tool_calls ────────────────────────────────────
        finalized_tool_calls: List[Dict[str, Any]] = []
        if final_message is not None:
            for block in getattr(final_message, "content", []):
                if getattr(block, "type", None) != "tool_use":
                    continue
                # P0 契约：getattr(block, "id", default) 对 block.id=None 不触发
                # default（只有 AttributeError 才触发）——必须 ensure_tool_id 兜底，
                # 否则 None 会顺着 assistant_msg(model_dump exclude_none) 被吞掉，
                # 只剩 tool_result 侧有 id → 历史回放时孤儿，触发 Anthropic 400
                finalized_tool_calls.append({
                    "id": ensure_tool_id(
                        getattr(block, "id", None),
                        origin="anthropic_client_final_block",
                    ),
                    "name": getattr(block, "name", "unknown"),
                    "input": _normalize_tool_input(
                        getattr(block, "input", {}),
                        getattr(block, "name", "unknown"),
                    ),
                })
        if not finalized_tool_calls:
            for idx in sorted(current_tool_calls.keys()):
                tc = current_tool_calls[idx]
                finalized_tool_calls.append({
                    "id": tc["id"],
                    "name": tc["name"],
                    "input": _normalize_tool_input(tc["input"], tc["name"]),
                })

        # ── 组装 assistant 消息（保留 thinking blocks 完整 — extended thinking 协议要求）
        # P0 契约：tool_use block 的 id 必须与 finalized_tool_calls 对齐。
        # model_dump(exclude_none=True) 会把 block.id=None 的字段吞掉，导致
        # assistant 侧缺 id 而 tool_result 侧有 synthesize id → 配对失真 400。
        # 按 final_message.content 中 tool_use block 的出现顺序与 finalized_tool_calls
        # zip，把 ensure_tool_id 的结果回填（顺序严格一致，构造时是同一循环）。
        assistant_content: List[Dict[str, Any]] = []
        if final_message is not None and getattr(final_message, "content", None):
            _tool_use_id_iter = iter(tc["id"] for tc in finalized_tool_calls)
            for block in final_message.content:
                if hasattr(block, "model_dump"):
                    block_dict = block.model_dump(exclude_none=True)
                elif hasattr(block, "dict"):
                    block_dict = block.dict(exclude_none=True)
                elif isinstance(block, dict):
                    block_dict = dict(block)
                else:
                    continue
                if block_dict.get("type") == "tool_use":
                    # 强制回填 ensure_tool_id 的结果（即使 model_dump 保留了原 id，
                    # 跟 finalized_tool_calls 保持 bit-identical；上游给 None 时也有值）
                    try:
                        block_dict["id"] = next(_tool_use_id_iter)
                    except StopIteration:
                        block_dict["id"] = ensure_tool_id(
                            block_dict.get("id"),
                            origin="anthropic_client_assistant_content",
                        )
                assistant_content.append(block_dict)
        else:
            # final_message 拿不到的兜底分支（极罕见）
            if current_content:
                assistant_content.append({"type": "text", "text": current_content})
            for tc in finalized_tool_calls:
                assistant_content.append({
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["name"],
                    "input": tc["input"],
                })

        assistant_msg = {"role": "assistant", "content": assistant_content}

        # ── 体感优化（2026-05-16）：tool_call 事件必须先于 dispatch_batch yield ──
        # 与 OpenAI 路径（llm_client.py:1786-1801）对齐，避免"先静默并发执行 N 秒、
        # 再 tool_call 和 tool_result 同时 flash"的死气沉沉体感。前端拿到 tool_call
        # 立刻插入 status=calling 的卡片，菊花在 dispatch_batch 真正并发执行的那
        # 1-3 秒可见——这是"爽感"的核心填充时间。subagent 路径的同款修复见
        # llm_client.py 配套 memory（55ab151a-...）。
        if stream:
            for tc in finalized_tool_calls:
                yield {
                    "type": "tool_call",
                    "name": tc["name"],
                    "input": tc["input"],
                    "tool_use_id": tc["id"],
                    "id": tc["id"],
                    "trace_id": tc["id"],
                }

        # ── 工具 dispatch（v2 C1' hook 已挂在 dispatch_batch 内，mw 11/12/13 真生效）
        _batch_view = [
            {"id": tc["id"], "name": tc["name"], "arguments": tc["input"]}
            for tc in finalized_tool_calls
        ]
        results_by_id = await self.tool_registry.dispatch_batch(
            _batch_view,
            agent_id=agent_id,
            session_id=session_id,
            llm_client=self,
        )
        tool_results: List[Dict[str, Any]] = []
        executed_tool_calls: List[Dict[str, Any]] = []
        executed_tool_results: List[str] = []
        spawn_dispatched = False
        # cline 流派 2：本轮所有 image 集中收集，循环末尾拼成独立 user message
        # 紧跟 tool_results user message 之后追加。事故根因 + 切派理由见
        # backend/llm_client.py:_split_text_and_image_blocks docstring。
        deferred_image_blocks: List[Dict[str, Any]] = []
        deferred_image_tool_names: List[str] = []
        deferred_image_tool_use_ids: List[str] = []

        for tc in finalized_tool_calls:
            fn_name = tc["name"]
            fn_args = tc["input"]
            tool_use_id = tc["id"]
            executed_tool_calls.append({"name": fn_name, "arguments": fn_args})

            res_entry = results_by_id.get(tool_use_id) or {}
            if "exception" in res_entry:
                tool_exc = res_entry["exception"]
                result = f"Error: {type(tool_exc).__name__}: {tool_exc}"
            else:
                result = res_entry.get("result", "")
            from backend.tools.builtin.clean_registry import format_tool_result_for_display
            # vision: read_file 图片返 _content_blocks 标记位（list[block]）。
            # cline 流派 2：image 拆走当 deferred user message，tool_result.content
            # 只放 text + anchor 字符串（避免反代 tool_result 路径吃图）。
            from backend.llm_client import (
                _extract_content_blocks_from_tool_result,
                _display_text_for_blocks,
                _split_text_and_image_blocks,
                _build_tool_image_user_message,
                _TOOL_RESULT_IMAGE_ANCHOR,
            )
            content_blocks = _extract_content_blocks_from_tool_result(result)
            if content_blocks is not None:
                display_result = _display_text_for_blocks(content_blocks, result)
                text_str, this_round_images = _split_text_and_image_blocks(content_blocks)
                if this_round_images:
                    tool_result_content: Any = (
                        (text_str or "(image attached)") + _TOOL_RESULT_IMAGE_ANCHOR
                    )
                    deferred_image_blocks.extend(this_round_images)
                    deferred_image_tool_names.append(fn_name)
                    if tool_use_id:
                        deferred_image_tool_use_ids.append(tool_use_id)
                else:
                    # 防御：标记位存在但拆完没图（罕见）
                    tool_result_content = text_str or result
            else:
                display_result = format_tool_result_for_display(fn_name, result)
                tool_result_content = result

            if stream:
                _ev: Dict[str, Any] = {
                    "type": "tool_result",
                    "name": fn_name,
                    "result": display_result,
                    "tool_use_id": tool_use_id,
                    "trace_id": tool_use_id,
                }
                yield _ev

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tc["id"],
                "content": tool_result_content,
            })
            executed_tool_results.append(str(result))

        tool_result_msg = {"role": "user", "content": tool_results}

        # cline 流派 2：本轮 deferred image user message（紧跟 tool_results 之后追加）
        tool_image_user_msg: Optional[Dict[str, Any]] = None
        if deferred_image_blocks:
            tool_image_user_msg = _build_tool_image_user_message(
                deferred_image_blocks,
                deferred_image_tool_names,
            )
            if stream:
                yield {
                    "type": "tool_image_attachment",
                    "tool_use_ids": list(deferred_image_tool_use_ids),
                    "tool_names": list(deferred_image_tool_names),
                    "content_blocks": tool_image_user_msg["content"],
                }

        yield {
            "type": "_round_done",
            "finish_reason": finish_reason,
            "has_tool_calls": True,
            "assistant_msg": assistant_msg,
            "tool_result_msg": tool_result_msg,
            "tool_image_user_msg": tool_image_user_msg,
            "executed_tool_calls": executed_tool_calls,
            "executed_tool_results": executed_tool_results,
            "spawn_dispatched": spawn_dispatched,
            "token_usage": round_token_usage,
        }

    async def _chat_with_tools(
        self,
        messages: List[Dict[str, Any]],
        stream: bool = True,
        agent_id: str = "default",
        session_id: Optional[str] = None,
        prompt_cache_info: Dict = None,
        allow_auth_retry: bool = True,
        chain_hook: Optional[Callable[[Dict[str, Any]], Awaitable[bool]]] = None,
        allowed_tools_override: Optional[List[str]] = None,
    ) -> AsyncIterator[Dict]:
        """带工具调用的聊天（多轮工具循环）

        v2 C2-c 改造（2026-04-30）：可选 ``chain_hook`` —— 每轮 _chat_one_round
        完成后调一次，签名 ``async (round_done: dict) -> bool``。返 True 时
        v1 立即终止本次工具循环（chain 决定 should_end，比 v1 自己的 stop 决策
        优先级高）。这让 v2 chain 的 mw 08 LoopDetection / mw 16 SubagentLimit /
        mw 17 Clarification 等 after_model 钩子真生效——通过反向注入而非 chain
        接管循环；老管家仍在跑循环但已经听 chain 指挥。

        v2 C2-b 改造（2026-04-30）：循环主体已抽到 ``_chat_one_round``；本方法
        保留循环外决策（budget / guard / spawn / convergence / max_rounds）+
        外层异常处理（auth retry / classify error）。事件流时序对外完全等价。

        Phase B/C 改造：兼容 list[ContentBlock] 形式的 content（v2 路径输出）。
        - system 消息：list content 时合并 text block；str content 时按原逻辑拼接
        - 非 system 消息：直接透传给 SDK（Anthropic 原生支持 list[ContentBlock]）
        - v2=0 时旧行为完全等价（content 永远是 str，走第一个分支）
        """
        # 分离 system 消息（详见 _split_system_and_chat_messages）
        system_prompt, chat_messages = _split_system_and_chat_messages(messages)

        # P2-C: prompt cache 由 AnthropicCachePlugin 接管。caller 直接透传
        # plain str + prompt_cache_info，plugin 在 ApiClient.send_stream 时
        # 把 system 转 list[block] + cache_control 结构。
        system_param = system_prompt

        # P1#4：按当前 agent_id 查白名单，未配置时返回 None → 不过滤（向后兼容全工具）
        # anthropic 路径直接拿 _chat_with_tools 的 agent_id 参数（不像 openai 走 ContextVar）
        # ── allowed_tools_override 优先级 ──
        # subagent engine 传了精确白名单时旁路 agent_manager。语义与 OpenAI 路径一致。
        if allowed_tools_override is not None:
            _allowed = list(allowed_tools_override)
        else:
            _allowed = None
            try:
                from backend.agent_config import get_agent_manager
                _allowed = get_agent_manager().get_allowed_tools(agent_id)
            except Exception as e:
                logger.warning("[anthropic_client] tool whitelist lookup failed, fallthrough: %s", e)

        # 获取工具定义
        tools = self.tool_registry.get_tool_definitions(allowed_tools=_allowed)
        anthropic_tools = None
        if tools:
            # Tool registry already returns Anthropic-native format
            anthropic_tools = tools

        # v2 C2-b：last_heartbeat / timeout / inner retry / stream 累积等
        # 单轮局部状态全部搬到 _chat_one_round 内部管理。
        # 这里只保留循环外决策需要的 guard / budget。
        guard = ToolLoopGuard.from_env("anthropic")
        budget = AggregateRuntimeBudget.from_env("anthropic")
        for round_num in range(guard.max_rounds):
            stop = budget.observe_before_round()
            if stop:
                final_response = await self._finalize_after_tool_loop(system_prompt, chat_messages, stop)
                yield build_tool_loop_notice_event(stop)
                yield {"type": "content", "text": final_response}
                return
            try:
                # v2 C2-b：调 _chat_one_round 跑单轮（_chat_one_round 内部含
                # inner retry / stream 累积 / token usage / 工具 dispatch）
                # _chat_one_round 不修改 chat_messages — 通过末尾 _round_done 事件
                # 回传 assistant_msg / tool_result_msg / executed_tool_calls 等
                round_done: Optional[Dict[str, Any]] = None
                async for event in self._chat_one_round(
                    chat_messages,
                    system_param,
                    anthropic_tools,
                    agent_id=agent_id,
                    session_id=session_id,
                    stream=stream,
                    round_num=round_num,
                    prompt_cache_info=prompt_cache_info,
                ):
                    if event.get("type") == "_round_done":
                        round_done = event
                        continue
                    yield event

                if round_done is None:
                    # _chat_one_round 应该总是 yield _round_done — 防御性兜底
                    return

                # === C2-c 反向注入：chain hook（after_model 钩子真生效）===
                # mw 08 LoopDetection / mw 14 TitleGen / mw 15 MemoryQueue /
                # mw 16 SubagentLimit / mw 17 Clarification 等 after_model 钩子
                # 都通过 chain_hook 在每轮跑；返 True 时 chain 决定 should_end，
                # v1 立即终止循环（优先级高于 v1 自己的 budget / guard 决策）。
                if chain_hook is not None:
                    try:
                        if await chain_hook(round_done):
                            return
                    except Exception:
                        # chain hook 异常不影响 v1 主路径；记日志（hook 内部
                        # 也应该 swallow，这里再保险一层）
                        import traceback
                        logger.error(
                            "[anthropic_client] chain_hook failed: %s",
                            traceback.format_exc(),
                        )
                # =============================================================

                if not round_done["has_tool_calls"]:
                    # 没工具，正常结束
                    return

                # 把 round 产出的 messages 加进对话上下文
                chat_messages.append(round_done["assistant_msg"])
                chat_messages.append(round_done["tool_result_msg"])
                # cline 流派 2：本轮工具产出的 image 单独成一条 user message 紧跟
                # tool_results 之后（如果存在）。convergence prompt 注入逻辑下面
                # 找的是 chat_messages[-1]，先 stop / spawn 决策结束、convergence
                # 注入完再 append image_msg，保持既有 chat_messages[-1] 语义不被
                # image_msg 干扰。
                _pending_image_user_msg = round_done.get("tool_image_user_msg")

                stop = guard.observe_round(
                    round_done["executed_tool_calls"],
                    round_done["executed_tool_results"],
                )
                if stop:
                    final_response = await self._finalize_after_tool_loop(system_prompt, chat_messages, stop)
                    yield build_tool_loop_notice_event(stop)
                    yield {"type": "content", "text": final_response}
                    return

                stop = budget.observe_tool_batch(
                    round_done["executed_tool_calls"],
                    round_done["executed_tool_results"],
                )
                if stop:
                    final_response = await self._finalize_after_tool_loop(system_prompt, chat_messages, stop)
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
                    # 把 convergence prompt 加到刚 append 的 tool_result_msg.content
                    tool_result_message = chat_messages[-1]
                    tool_result_message["content"] = list(tool_result_message["content"]) + [
                        {"type": "text", "text": convergence.prompt_message}
                    ]
                # cline 流派 2：convergence 注入完毕后再 append image user message，
                # 让模型在新一轮看到 image-in-user-message 合法位置（避免被反代
                # tool_result 路径吃图）。messages 末尾顺序：[..., assistant,
                # tool_result_user(可能含 convergence prompt), image_user]。
                if _pending_image_user_msg is not None:
                    chat_messages.append(_pending_image_user_msg)
                continue  # 继续下一轮

            except Exception as e:
                if allow_auth_retry and self._should_retry_with_auth_token(e):
                    self._switch_auth_mode("auth_token")
                    async for event in self._chat_with_tools(messages, stream, agent_id=agent_id, session_id=session_id, prompt_cache_info=prompt_cache_info, allow_auth_retry=False):
                        yield event
                    return
                if isinstance(e, LLMError):
                    raise
                if isinstance(e, json.JSONDecodeError):
                    raise LLMError(f"Anthropic tool-call JSON parse failed: {e}", LLMErrorCode.RESPONSE_FORMAT, retryable=False) from e
                if hasattr(e, "status_code"):
                    raise classify_anthropic_error(e.status_code, str(e))
                if _is_anthropic_connect_error(e):
                    raise LLMError(
                        f"上游连接失败（可能是代理 / 反代 / 网络抖动）。请检查 HTTP_PROXY/HTTPS_PROXY、代理是否在线，或先关闭代理重试。原始错误：{type(e).__name__}: {e}",
                        LLMErrorCode.NETWORK_ERROR,
                        retryable=True,
                    ) from e
                raise LLMError(str(e), LLMErrorCode.NETWORK_ERROR, retryable=True)

        # 超过最大轮数
        stop = guard.max_rounds_stop()
        final_response = await self._finalize_after_tool_loop(system_prompt, chat_messages, stop)
        yield build_tool_loop_notice_event(stop)
        yield {"type": "content", "text": final_response}
