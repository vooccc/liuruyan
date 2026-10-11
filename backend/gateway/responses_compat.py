"""OpenAI Responses API ↔ Chat Completions 翻译层（Phase 1）。

Codex CLI 0.30+ 强制 Responses API，柳如烟 gateway 内部仍走 chat/completions
（fallback / health cache / alias / audit 全部围绕 chat 协议）。此模块在
gateway 边界做双向翻译，让上游不变、客户端可用。

设计依据：`docs/architecture_spec.md`
参考实现：`references/api-compat` 的反向镜像

公开 API：
- ``req_to_chat(responses_req, loss_log=None)``  → ``(chat_req, response_id, created_at, model)``
  传入可变 ``loss_log: list[str]`` 可收集静默丢/忽略的字段，供调用方写入 audit.
- ``resp_to_resp(chat_resp, *, response_id, created_at, model)`` → responses dict
- ``chat_stream_to_responses_stream(chat_iter, *, response_id, created_at, model)``
  → async iter of SSE bytes（含 event/data 双行）
- ``ResponsesTranslationError``：失真字段拒绝时抛，路由层捕获返 400

纪律：
1. 翻译失败一律返 400，不静默丢失（built_in_tools / web_search / file_search）
2. ``metadata`` / ``store`` / ``previous_response_id`` / ``truncation`` 静默忽略
3. 流式 sequence_number 严格递增（codex 严格校验）
4. 所有 item id 用 ``msg_/fc_/rs_`` 前缀
5. data 行的 JSON 内**也要有 type 字段重复 event 名**（OpenAI 官方实现如此）
6. 静默丢/忽略点同时 stderr warn + ``loss_log.append(tag)``。tag 格式：
   - ``builtin_tool:<ttype>``　   —— web_search/file_search/code_interpreter 等丢弃
   - ``namespace_invalid:<name>`` —— namespace 容器 schema 不合法
   - ``unknown_tool_type:<ttype>``—— 未知 tool.type 丢弃
   - ``ignored:<field>``       —— metadata/store/previous_response_id/truncation/include
   - ``ignored:input_reasoning_history`` —— input 里的 reasoning item 丢失
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ─────────────────────── 异常 ───────────────────────


class ResponsesTranslationError(Exception):
    """翻译失败 → 路由层用 status/code/message 返 400。"""

    def __init__(self, *, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# ─────────────────────── id 生成 ───────────────────────


def _make_id(prefix: str) -> str:
    """OpenAI 风格：`<prefix>_<hex24>`。"""
    return f"{prefix}_{secrets.token_hex(12)}"


def make_response_id() -> str:
    return _make_id("resp")


def now_unix() -> int:
    return int(time.time())


# ─────────────────────── 请求方向：Responses → Chat ───────────────────────


_RESPONSES_BUILTIN_TYPES = {
    "web_search",
    "web_search_preview",
    "file_search",
    "code_interpreter",
    "computer_use",
    "computer_use_preview",
}
"""Responses 原生内置工具。

这些工具在 chat-completions vendor 上**天然不支持**（不是翻译失真，是平台差异）。
策略：**静默丢弃 + stderr 警告**，让请求继续进行；模型看不到这些工具自然不会调用，
等价于"该 vendor 没有此能力"。

为什么不 400 拒：codex 0.30+ 启动时默认会在 ``tools`` 数组里塞 ``web_search``——
若 hard-fail，所有 codex 第一次对话都会被柳如烟 gateway 阻断。ccswitch 那边能跑通
正是因为不做翻译，上游直接处理或同样静默丢。
"""


def req_to_chat(
    req: Dict[str, Any],
    *,
    loss_log: Optional[List[str]] = None,
) -> Tuple[Dict[str, Any], str, int, str]:
    """Responses request → Chat Completions request。

    返回 ``(chat_req, response_id, created_at, model)``。response_id/created_at
    由本函数生成，路由层后续翻译响应时复用，保证 codex 拿到的 id 稳定。

    ``loss_log`` 传入后，静默丢/忽略的字段会被 append 进去（tag 格式见模块 docstring）。
    不传也不会报错——老调用方（以及单测）都能照原样子走。
    """
    if not isinstance(req, dict):
        raise ResponsesTranslationError(
            code="invalid_request", message="request body must be a JSON object"
        )

    # —— 失真字段：built_in_tools 一律拒
    if "built_in_tools" in req and req["built_in_tools"]:
        raise ResponsesTranslationError(
            code="unsupported_field",
            message="`built_in_tools` not supported by liuruyan gateway translation",
        )

    model = req.get("model")
    if not isinstance(model, str) or not model:
        raise ResponsesTranslationError(
            code="invalid_request", message="`model` is required and must be a string"
        )

    chat: Dict[str, Any] = {"model": model}

    # —— stream / 温度 / top_p
    if "stream" in req:
        chat["stream"] = bool(req["stream"])
    for k in ("temperature", "top_p", "user", "parallel_tool_calls"):
        if k in req and req[k] is not None:
            chat[k] = req[k]

    # —— max_output_tokens → max_completion_tokens（OpenAI 新模型 / 兼容老的 max_tokens）
    if isinstance(req.get("max_output_tokens"), (int, float)):
        chat["max_completion_tokens"] = int(req["max_output_tokens"])

    # —— reasoning.effort → reasoning_effort（非标准；上游不支持则被透传忽略）
    reasoning = req.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort") in ("low", "medium", "high"):
        chat["reasoning_effort"] = reasoning["effort"]

    # —— text.format → response_format
    text_obj = req.get("text")
    if isinstance(text_obj, dict):
        fmt = text_obj.get("format")
        if isinstance(fmt, dict):
            chat["response_format"] = _translate_text_format_to_response_format(fmt)

    # —— tools / tool_choice
    if isinstance(req.get("tools"), list):
        # 单个 Responses tool 可能展开为多个 Chat function tool（codex namespace 容器）
        chat_tools: List[Dict[str, Any]] = []
        for t in req["tools"]:
            chat_tools.extend(_translate_tool(t, loss_log=loss_log))
        chat["tools"] = chat_tools
    if "tool_choice" in req and req["tool_choice"] is not None:
        chat["tool_choice"] = _translate_tool_choice(req["tool_choice"])

    # —— instructions + input → messages
    messages: List[Dict[str, Any]] = []
    instructions = req.get("instructions")
    if isinstance(instructions, str) and instructions:
        messages.append({"role": "system", "content": instructions})

    raw_input = req.get("input")
    if raw_input is None:
        # OpenAI 允许空 input（仅 instructions）；至少给一个 user 占位防 chat 上游拒
        if not messages:
            raise ResponsesTranslationError(
                code="invalid_request",
                message="at least one of `input` / `instructions` is required",
            )
    elif isinstance(raw_input, str):
        messages.append({"role": "user", "content": raw_input})
    elif isinstance(raw_input, list):
        messages.extend(
            _translate_input_items(raw_input, system_buf=messages, loss_log=loss_log)
        )
    else:
        raise ResponsesTranslationError(
            code="invalid_request",
            message="`input` must be a string or an array of items",
        )

    chat["messages"] = messages

    # 静默忽略字段（不报错 + audit 记一笔）：metadata / store / previous_response_id /
    # truncation / include / reasoning.encrypted_content 等。调用方拿到 loss_log 之后
    # 在 audit 里能看到“该请求丢过 metadata”，便于事后定位一些“为什么上游没看到该字段”。
    if loss_log is not None:
        for fld in ("metadata", "store", "previous_response_id", "truncation", "include"):
            if fld in req and req[fld] is not None and req[fld] != "":
                loss_log.append(f"ignored:{fld}")

    response_id = make_response_id()
    created_at = now_unix()
    return chat, response_id, created_at, model


def _translate_text_format_to_response_format(fmt: Dict[str, Any]) -> Dict[str, Any]:
    """Responses text.format → Chat response_format。"""
    ftype = fmt.get("type")
    if ftype == "text":
        return {"type": "text"}
    if ftype == "json_object":
        return {"type": "json_object"}
    if ftype == "json_schema":
        schema_inner = {
            "name": fmt.get("name") or "schema",
            "schema": fmt.get("schema") or {},
        }
        if "strict" in fmt:
            schema_inner["strict"] = bool(fmt["strict"])
        if isinstance(fmt.get("description"), str):
            schema_inner["description"] = fmt["description"]
        return {"type": "json_schema", "json_schema": schema_inner}
    # 未知类型——保守做法：直接透传，让上游报错（不是我们能判定的失真）
    return dict(fmt)


def _translate_tool(
    tool: Dict[str, Any],
    *,
    loss_log: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Responses tool → Chat tool（list；通常 1 项，namespace 容器会展开为多项）。

    支持的 ttype：

    - ``function`` / 缺省 → 单个 ``{type:"function", function:{...}}``
    - ``namespace`` → codex 0.30+ 把 MCP servers 包成此容器；展开内部 ``tools[]``,
      子工具名加 ``<namespace>__`` 前缀避免冲突。容器自身的 ``description`` 丢失（chat 协议无对应位）。
    - ``custom`` → codex 自定义工具壳；尝试当 function 处理（结构通常一致）
    - Responses 内置工具（``web_search`` / ``file_search`` / ``code_interpreter`` /
      ``computer_use``）→ **静默丢弃 + warn**（chat-completions vendor 天然不支持；
      模型看不到自不会调用，等价于"该 vendor 没此能力"）。**不能 400 拒**，否则
      codex 0.30+ 启动默认带 ``web_search`` 会被全量阻断。
    - 其它 codex 内部类型（``local_shell`` 等）→ best-effort：若有 ``name``+``parameters``
      当 function 处理；否则**静默丢弃 + stderr warn**

    ``loss_log`` 传入后，每一处静默丢/降级会写一条 tag 进去（格式见模块 docstring）。
    """
    if not isinstance(tool, dict):
        raise ResponsesTranslationError(
            code="invalid_request", message="`tools[]` items must be objects"
        )
    ttype = tool.get("type")

    if ttype in _RESPONSES_BUILTIN_TYPES:
        # 静默丢弃：chat-completions vendor 不支持这些原生工具。
        # 不是翻译失真——是平台能力差异，模型不会知道这工具存在。
        logger.warning(
            "[RESPONSES-COMPAT] dropping Responses built-in tool '%s' "
            "(chat-completions vendors don't support it; switch to a Responses-native vendor if you need this)",
            ttype,
        )
        if loss_log is not None:
            loss_log.append(f"builtin_tool:{ttype}")
        return []

    if ttype == "function" or ttype is None:
        return [_build_chat_function_tool(tool)]

    if ttype == "namespace":
        # codex 0.30+ MCP namespace 容器。可能的 schema（已知变体）：
        #   {type:"namespace", namespace:"<name>", tools:[{type:"function", ...}, ...]}
        #   {type:"namespace", name:"<name>", tools:[...]}
        ns_name = tool.get("namespace") or tool.get("name") or ""
        sub_tools = tool.get("tools")
        if not isinstance(sub_tools, list):
            logger.warning(
                "[RESPONSES-COMPAT] namespace tool without `.tools` list dropped: keys=%s",
                list(tool.keys()),
            )
            if loss_log is not None:
                loss_log.append(f"namespace_invalid:{ns_name or '<unnamed>'}")
            return []
        flat: List[Dict[str, Any]] = []
        prefix = f"{ns_name}__" if ns_name else ""
        for sub in sub_tools:
            if not isinstance(sub, dict):
                continue
            sub_type = sub.get("type")
            if sub_type == "function" or sub_type is None:
                fn_tool = _build_chat_function_tool(sub)
                # 加 namespace 前缀避免名字冲突（codex 接 tool_call 回包时也会用前缀名）
                if prefix and isinstance(fn_tool["function"].get("name"), str):
                    fn_tool["function"]["name"] = prefix + fn_tool["function"]["name"]
                flat.append(fn_tool)
            else:
                # namespace 内嵌非 function（理论上不会有）→ 静默丢
                logger.warning(
                    "[RESPONSES-COMPAT] namespace.%s contains non-function sub-tool type='%s'; dropped",
                    ns_name, sub_type,
                )
                if loss_log is not None:
                    loss_log.append(
                        f"namespace_invalid:{ns_name or '<unnamed>'}/sub_type={sub_type}"
                    )
        return flat

    if ttype == "custom":
        # codex 自定义工具壳；schema 与 function 兼容
        return [_build_chat_function_tool(tool)]

    # 其它未知 type —— best-effort：有 name+parameters 当 function；否则丢弃 + warn
    if isinstance(tool.get("name"), str) and tool.get("name"):
        logger.warning(
            "[RESPONSES-COMPAT] unknown tool.type='%s', best-effort treat as function (name=%s)",
            ttype, tool.get('name'),
        )
        # best-effort 透传不记 loss——上游能识别就当 function 用，谈不上"丢"
        return [_build_chat_function_tool(tool)]

    logger.warning(
        "[RESPONSES-COMPAT] unknown tool.type='%s' without name; dropped. keys=%s",
        ttype, list(tool.keys()),
    )
    if loss_log is not None:
        loss_log.append(f"unknown_tool_type:{ttype or '<missing>'}")
    return []


def _build_chat_function_tool(tool: Dict[str, Any]) -> Dict[str, Any]:
    """从 Responses function 壳子构造 Chat function tool。

    Responses: ``{type:"function", name, description, parameters, strict?}``
    Chat:      ``{type:"function", function:{name, description, parameters, strict?}}``
    """
    fn: Dict[str, Any] = {}
    if "name" in tool:
        fn["name"] = tool["name"]
    if "description" in tool:
        fn["description"] = tool["description"]
    if "parameters" in tool:
        fn["parameters"] = tool["parameters"]
    if "strict" in tool:
        fn["strict"] = tool["strict"]
    return {"type": "function", "function": fn}


def _translate_tool_choice(choice: Any) -> Any:
    """Responses tool_choice → Chat tool_choice。"""
    if isinstance(choice, str):
        return choice  # "auto" / "required" / "none"
    if isinstance(choice, dict):
        if choice.get("type") == "function":
            name = choice.get("name") or (choice.get("function") or {}).get("name")
            return {"type": "function", "function": {"name": name}}
        # 其它形式直接透传
        return choice
    return choice


# ─────────────────────── input items → chat messages ───────────────────────


def _translate_input_items(
    items: List[Any],
    *,
    system_buf: List[Dict[str, Any]],
    loss_log: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """逐 item 翻译；assistant + function_call 合并为单条 chat assistant。

    ``loss_log`` 传入后，input 里**首条** reasoning item（chat 协议无等价物）被丢弃时
    会写一条 ``ignored:input_reasoning_history``——多条只记一次，避免一次请求里上下文很长
    时把 audit 撑爆。
    """
    reasoning_dropped_logged = False
    out: List[Dict[str, Any]] = []
    cur_assistant: Optional[Dict[str, Any]] = None

    def _flush_assistant() -> None:
        nonlocal cur_assistant
        if cur_assistant is not None:
            out.append(cur_assistant)
            cur_assistant = None

    for item in items:
        if not isinstance(item, dict):
            raise ResponsesTranslationError(
                code="invalid_request",
                message="`input[]` items must be objects",
            )
        itype = item.get("type")
        role = item.get("role")

        # 1. function_call —— 紧跟 assistant 合并；若没 open 的 assistant 则起一个空壳
        if itype == "function_call":
            if cur_assistant is None:
                cur_assistant = {"role": "assistant", "content": None, "tool_calls": []}
            tcs = cur_assistant.setdefault("tool_calls", [])
            tcs.append({
                "id": item.get("call_id") or item.get("id") or _make_id("call"),
                "type": "function",
                "function": {
                    "name": item.get("name") or "",
                    "arguments": item.get("arguments") or "",
                },
            })
            continue

        # 2. function_call_output → chat tool message
        if itype == "function_call_output":
            _flush_assistant()
            content = item.get("output")
            if isinstance(content, list):
                # output 可能是 content parts list
                content = _join_text_parts(content)
            out.append({
                "role": "tool",
                "tool_call_id": item.get("call_id") or "",
                "content": content if isinstance(content, str) else json.dumps(content),
            })
            continue

        # 3. reasoning item（historic CoT）→ 静默丢失（chat 没有等价物）
        if itype == "reasoning":
            if loss_log is not None and not reasoning_dropped_logged:
                loss_log.append("ignored:input_reasoning_history")
                reasoning_dropped_logged = True
            continue

        # 4. role message
        if role == "developer":
            # developer ≈ system；merge 到 system_buf 已有 system 或追加一条
            _flush_assistant()
            text = _extract_text(item.get("content"))
            if system_buf and system_buf[0].get("role") == "system":
                system_buf[0]["content"] = (system_buf[0]["content"] or "") + "\n" + text
            else:
                # 还没有 system 时，插到 messages 最前
                system_buf.insert(0, {"role": "system", "content": text})
            continue

        if role == "user":
            _flush_assistant()
            out.append({
                "role": "user",
                "content": _translate_user_content(item.get("content")),
            })
            continue

        if role == "assistant":
            _flush_assistant()
            cur_assistant = {
                "role": "assistant",
                "content": _extract_text(item.get("content")),
            }
            continue

        if role == "system":
            _flush_assistant()
            out.append({"role": "system", "content": _extract_text(item.get("content"))})
            continue

        if role == "tool":
            # 兼容某些客户端直接传 chat 风格的 tool message
            _flush_assistant()
            out.append({
                "role": "tool",
                "tool_call_id": item.get("tool_call_id") or "",
                "content": _extract_text(item.get("content")),
            })
            continue

        # 5. 顶层是 input_text/input_image（无 role 包装）——视为单条 user
        if itype in ("input_text", "input_image", "input_audio", "input_file"):
            _flush_assistant()
            out.append({
                "role": "user",
                "content": _translate_user_content([item]),
            })
            continue

        # 未知 item：保守丢弃（不抛错，让客户端继续；翻译层应宽容上游差异）
        # 在 audit 层应能看到原始 item，便于排查
        continue

    _flush_assistant()
    return out


def _translate_user_content(content: Any) -> Any:
    """user content：string 直通；array 翻译每个 part。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts: List[Dict[str, Any]] = []
    for p in content:
        if not isinstance(p, dict):
            continue
        ptype = p.get("type")
        if ptype == "input_text" or ptype == "text":
            parts.append({"type": "text", "text": p.get("text") or ""})
        elif ptype == "input_image" or ptype == "image_url":
            url = p.get("image_url")
            if isinstance(url, dict):
                url = url.get("url")
            if isinstance(url, str):
                parts.append({"type": "image_url", "image_url": {"url": url}})
        elif ptype == "input_audio" or ptype == "input_file":
            # 透传保留字段；上游不支持自然报错
            parts.append({"type": ptype, **{k: v for k, v in p.items() if k != "type"}})
        else:
            # 未知 part：透传
            parts.append(p)
    # 单纯文本时降级回 string，更兼容老上游
    if len(parts) == 1 and parts[0].get("type") == "text":
        return parts[0]["text"]
    return parts


def _extract_text(content: Any) -> str:
    """从 content（string | array of parts）抽出纯文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return _join_text_parts(content)
    return str(content)


def _join_text_parts(parts: List[Any]) -> str:
    buf: List[str] = []
    for p in parts:
        if isinstance(p, dict):
            t = p.get("text") or p.get("output_text")
            if isinstance(t, str):
                buf.append(t)
        elif isinstance(p, str):
            buf.append(p)
    return "".join(buf)


# ─────────────────────── 响应方向：Chat → Responses（非流式） ───────────────────────


_FINISH_REASON_STATUS: Dict[str, Tuple[str, Optional[Dict[str, str]]]] = {
    "stop": ("completed", None),
    "tool_calls": ("completed", None),
    "function_call": ("completed", None),
    "length": ("incomplete", {"reason": "max_output_tokens"}),
    "content_filter": ("incomplete", {"reason": "content_filter"}),
}


def resp_to_resp(
    chat_resp: Dict[str, Any],
    *,
    response_id: str,
    created_at: int,
    model: str,
) -> Dict[str, Any]:
    """Chat completion 非流式响应 → Responses 非流式响应。"""
    choices = chat_resp.get("choices") or []
    msg = (choices[0] or {}).get("message") if choices else {}
    msg = msg or {}
    finish_reason = (choices[0] or {}).get("finish_reason") if choices else None
    status, incomplete = _FINISH_REASON_STATUS.get(finish_reason or "stop", ("completed", None))

    output: List[Dict[str, Any]] = []

    # 1. reasoning（DeepSeek/Qwen 非标准 reasoning_content）→ reasoning item（在 message 之前）
    rc = msg.get("reasoning_content")
    if isinstance(rc, str) and rc:
        output.append({
            "id": _make_id("rs"),
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": rc}],
        })

    # 2. 文本部分 → message item
    text = msg.get("content")
    if isinstance(text, list):
        text = _join_text_parts(text)
    if isinstance(text, str) and text:
        output.append({
            "id": _make_id("msg"),
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{
                "type": "output_text",
                "text": text,
                "annotations": [],
            }],
        })

    # 3. tool_calls → function_call items
    for tc in msg.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        output.append({
            "id": _make_id("fc"),
            "type": "function_call",
            "call_id": tc.get("id") or "",
            "name": fn.get("name") or "",
            "arguments": fn.get("arguments") or "",
            "status": "completed",
        })

    resp: Dict[str, Any] = {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "model": chat_resp.get("model") or model,
        "status": status,
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "temperature": 1.0,
        "top_p": 1.0,
    }
    if incomplete is not None:
        resp["incomplete_details"] = incomplete
    if chat_resp.get("system_fingerprint"):
        resp["system_fingerprint"] = chat_resp["system_fingerprint"]

    usage = chat_resp.get("usage")
    if isinstance(usage, dict):
        resp["usage"] = _translate_usage(usage)

    return resp


def _translate_usage(usage: Dict[str, Any]) -> Dict[str, Any]:
    """Chat usage → Responses usage（字段名变换）。"""
    out: Dict[str, Any] = {
        "input_tokens": int(usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }
    pd = usage.get("prompt_tokens_details")
    if isinstance(pd, dict):
        out["input_tokens_details"] = {
            k: v for k, v in pd.items() if isinstance(v, (int, float))
        }
    cd = usage.get("completion_tokens_details")
    if isinstance(cd, dict):
        out["output_tokens_details"] = {
            k: v for k, v in cd.items() if isinstance(v, (int, float))
        }
    return out


# ─────────────────────── 流式：Chat SSE → Responses Event Stream ───────────────────────


@dataclass
class _ToolCallState:
    output_index: int
    item_id: str
    call_id: str = ""
    name: str = ""
    args_buffer: str = ""


@dataclass
class _StreamState:
    response_id: str
    created_at: int
    model: str
    sequence_number: int = 0

    sent_created: bool = False

    # message item
    text_item_open: bool = False
    text_output_index: int = -1
    text_item_id: str = ""
    text_buffer: str = ""

    # reasoning item
    reasoning_item_open: bool = False
    reasoning_output_index: int = -1
    reasoning_item_id: str = ""
    reasoning_buffer: str = ""

    # tool_calls：按 chat 的 index 寻址（chat 流式 tool_calls[*].index）
    tool_calls: Dict[int, _ToolCallState] = field(default_factory=dict)

    next_output_index: int = 0

    finish_reason: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None

    def next_seq(self) -> int:
        n = self.sequence_number
        self.sequence_number += 1
        return n

    def alloc_output_index(self) -> int:
        n = self.next_output_index
        self.next_output_index += 1
        return n


def _sse_event(event_name: str, data_obj: Dict[str, Any]) -> bytes:
    """编码 SSE 一条事件。

    data 行的 JSON **必须**包含 `type` 字段重复 event 名——OpenAI 官方实现如此，
    codex 解析依赖此字段。
    """
    payload = {"type": event_name, **data_obj}
    return (
        f"event: {event_name}\n"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    ).encode("utf-8")


def _initial_response_snapshot(state: _StreamState, status: str = "in_progress") -> Dict[str, Any]:
    return {
        "id": state.response_id,
        "object": "response",
        "created_at": state.created_at,
        "model": state.model,
        "status": status,
        "output": [],
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "temperature": 1.0,
        "top_p": 1.0,
    }


async def chat_stream_to_responses_stream(
    chat_chunks: AsyncIterator[bytes],
    *,
    response_id: str,
    created_at: int,
    model: str,
) -> AsyncIterator[bytes]:
    """把上游 chat SSE 字节流翻成 responses 事件流字节流。

    输入 ``chat_chunks`` 是按行/帧的字节块（直接来自 forward_with_fallback_stream）。
    我们先 line-buffer 还原出每条 ``data: <json>`` 的 JSON 对象，再驱动状态机。
    """
    state = _StreamState(response_id=response_id, created_at=created_at, model=model)
    buf = b""

    async for raw in chat_chunks:
        if not raw:
            continue
        buf += raw
        # 按 SSE 标准 \n\n 分帧
        while b"\n\n" in buf:
            frame, buf = buf.split(b"\n\n", 1)
            for ev_bytes in _drive_one_frame(state, frame):
                yield ev_bytes

    # buf 残留也尝试处理（部分上游不严格补 \n\n）
    if buf.strip():
        for ev_bytes in _drive_one_frame(state, buf):
            yield ev_bytes

    # 收尾：关闭所有 open items
    for ev_bytes in _finalize(state):
        yield ev_bytes


def _drive_one_frame(state: _StreamState, frame: bytes) -> Iterable[bytes]:
    """处理一帧 SSE（多行 data: 拼起来），驱动状态机产事件。"""
    data_lines: List[str] = []
    for line in frame.split(b"\n"):
        s = line.decode("utf-8", errors="replace").strip()
        if not s or s.startswith(":"):
            continue
        if s.startswith("data:"):
            data_lines.append(s[5:].lstrip())
    if not data_lines:
        return
    data_text = "\n".join(data_lines)
    if data_text == "[DONE]":
        return  # _finalize 已发 response.completed
    try:
        chunk = json.loads(data_text)
    except json.JSONDecodeError:
        return
    if not isinstance(chunk, dict):
        return
    yield from _process_chat_chunk(state, chunk)


def _process_chat_chunk(state: _StreamState, chunk: Dict[str, Any]) -> Iterable[bytes]:
    # 首次：发 response.created + response.in_progress
    if not state.sent_created:
        state.sent_created = True
        yield _sse_event("response.created", {
            "sequence_number": state.next_seq(),
            "response": _initial_response_snapshot(state, status="in_progress"),
        })
        yield _sse_event("response.in_progress", {
            "sequence_number": state.next_seq(),
            "response": _initial_response_snapshot(state, status="in_progress"),
        })

    choices = chunk.get("choices") or []
    if not choices:
        # 末尾 usage-only chunk（OpenAI stream_options: include_usage）
        if isinstance(chunk.get("usage"), dict):
            state.usage = chunk["usage"]
        return

    choice0 = choices[0] or {}
    delta = choice0.get("delta") or {}

    # 1. reasoning_content（DeepSeek/Qwen 非标准）
    rc = delta.get("reasoning_content")
    if isinstance(rc, str) and rc:
        if not state.reasoning_item_open:
            state.reasoning_item_open = True
            state.reasoning_item_id = _make_id("rs")
            state.reasoning_output_index = state.alloc_output_index()
            yield _sse_event("response.output_item.added", {
                "sequence_number": state.next_seq(),
                "output_index": state.reasoning_output_index,
                "item": {
                    "id": state.reasoning_item_id,
                    "type": "reasoning",
                    "summary": [],
                },
            })
        state.reasoning_buffer += rc
        yield _sse_event("response.reasoning_summary_text.delta", {
            "sequence_number": state.next_seq(),
            "item_id": state.reasoning_item_id,
            "output_index": state.reasoning_output_index,
            "summary_index": 0,
            "delta": rc,
        })

    # 2. content（文本）
    text_delta = delta.get("content")
    if isinstance(text_delta, list):
        # 极少数上游用 parts 数组——抽出 text
        text_delta = _join_text_parts(text_delta)
    if isinstance(text_delta, str) and text_delta:
        if not state.text_item_open:
            state.text_item_open = True
            state.text_item_id = _make_id("msg")
            state.text_output_index = state.alloc_output_index()
            yield _sse_event("response.output_item.added", {
                "sequence_number": state.next_seq(),
                "output_index": state.text_output_index,
                "item": {
                    "id": state.text_item_id,
                    "type": "message",
                    "role": "assistant",
                    "status": "in_progress",
                    "content": [],
                },
            })
            yield _sse_event("response.content_part.added", {
                "sequence_number": state.next_seq(),
                "item_id": state.text_item_id,
                "output_index": state.text_output_index,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            })
        state.text_buffer += text_delta
        yield _sse_event("response.output_text.delta", {
            "sequence_number": state.next_seq(),
            "item_id": state.text_item_id,
            "output_index": state.text_output_index,
            "content_index": 0,
            "delta": text_delta,
        })

    # 3. tool_calls
    for tc_delta in delta.get("tool_calls") or []:
        if not isinstance(tc_delta, dict):
            continue
        chat_idx = tc_delta.get("index")
        if not isinstance(chat_idx, int):
            chat_idx = 0
        tc_state = state.tool_calls.get(chat_idx)
        fn_delta = tc_delta.get("function") or {}
        if tc_state is None:
            tc_state = _ToolCallState(
                output_index=state.alloc_output_index(),
                item_id=_make_id("fc"),
                call_id=tc_delta.get("id") or "",
                name=fn_delta.get("name") or "",
            )
            state.tool_calls[chat_idx] = tc_state
            yield _sse_event("response.output_item.added", {
                "sequence_number": state.next_seq(),
                "output_index": tc_state.output_index,
                "item": {
                    "id": tc_state.item_id,
                    "type": "function_call",
                    "call_id": tc_state.call_id,
                    "name": tc_state.name,
                    "arguments": "",
                    "status": "in_progress",
                },
            })
        else:
            # 后续 delta 可能补 id / name（个别 provider 拆得很碎）
            if not tc_state.call_id and tc_delta.get("id"):
                tc_state.call_id = tc_delta["id"]
            if not tc_state.name and fn_delta.get("name"):
                tc_state.name = fn_delta["name"]

        args_delta = fn_delta.get("arguments") or ""
        if args_delta:
            tc_state.args_buffer += args_delta
            yield _sse_event("response.function_call_arguments.delta", {
                "sequence_number": state.next_seq(),
                "item_id": tc_state.item_id,
                "output_index": tc_state.output_index,
                "delta": args_delta,
            })

    # 4. finish_reason
    if choice0.get("finish_reason"):
        state.finish_reason = choice0["finish_reason"]

    # 5. usage 同帧也可能带
    if isinstance(chunk.get("usage"), dict):
        state.usage = chunk["usage"]


def _finalize(state: _StreamState) -> Iterable[bytes]:
    """关闭所有 open items + 发 response.completed。"""
    # 若上游一帧都没来（极端早期错误），至少给客户端一个 created+completed 避免悬挂
    if not state.sent_created:
        state.sent_created = True
        yield _sse_event("response.created", {
            "sequence_number": state.next_seq(),
            "response": _initial_response_snapshot(state, status="in_progress"),
        })

    # reasoning 收尾
    if state.reasoning_item_open:
        yield _sse_event("response.reasoning_summary_text.done", {
            "sequence_number": state.next_seq(),
            "item_id": state.reasoning_item_id,
            "output_index": state.reasoning_output_index,
            "summary_index": 0,
            "text": state.reasoning_buffer,
        })
        yield _sse_event("response.output_item.done", {
            "sequence_number": state.next_seq(),
            "output_index": state.reasoning_output_index,
            "item": {
                "id": state.reasoning_item_id,
                "type": "reasoning",
                "summary": [{
                    "type": "summary_text",
                    "text": state.reasoning_buffer,
                }],
            },
        })

    # message 收尾
    if state.text_item_open:
        yield _sse_event("response.output_text.done", {
            "sequence_number": state.next_seq(),
            "item_id": state.text_item_id,
            "output_index": state.text_output_index,
            "content_index": 0,
            "text": state.text_buffer,
        })
        yield _sse_event("response.content_part.done", {
            "sequence_number": state.next_seq(),
            "item_id": state.text_item_id,
            "output_index": state.text_output_index,
            "content_index": 0,
            "part": {
                "type": "output_text",
                "text": state.text_buffer,
                "annotations": [],
            },
        })
        yield _sse_event("response.output_item.done", {
            "sequence_number": state.next_seq(),
            "output_index": state.text_output_index,
            "item": {
                "id": state.text_item_id,
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{
                    "type": "output_text",
                    "text": state.text_buffer,
                    "annotations": [],
                }],
            },
        })

    # tool_calls 收尾（按 chat_idx 升序确保稳定）
    for chat_idx in sorted(state.tool_calls.keys()):
        tc_state = state.tool_calls[chat_idx]
        yield _sse_event("response.function_call_arguments.done", {
            "sequence_number": state.next_seq(),
            "item_id": tc_state.item_id,
            "output_index": tc_state.output_index,
            "arguments": tc_state.args_buffer,
        })
        yield _sse_event("response.output_item.done", {
            "sequence_number": state.next_seq(),
            "output_index": tc_state.output_index,
            "item": {
                "id": tc_state.item_id,
                "type": "function_call",
                "call_id": tc_state.call_id,
                "name": tc_state.name,
                "arguments": tc_state.args_buffer,
                "status": "completed",
            },
        })

    # 终响应
    status, incomplete = _FINISH_REASON_STATUS.get(
        state.finish_reason or "stop", ("completed", None)
    )
    final_output: List[Dict[str, Any]] = []
    if state.reasoning_item_open:
        final_output.append({
            "id": state.reasoning_item_id,
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": state.reasoning_buffer}],
        })
    if state.text_item_open:
        final_output.append({
            "id": state.text_item_id,
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{
                "type": "output_text",
                "text": state.text_buffer,
                "annotations": [],
            }],
        })
    for chat_idx in sorted(state.tool_calls.keys()):
        tc_state = state.tool_calls[chat_idx]
        final_output.append({
            "id": tc_state.item_id,
            "type": "function_call",
            "call_id": tc_state.call_id,
            "name": tc_state.name,
            "arguments": tc_state.args_buffer,
            "status": "completed",
        })

    final_response: Dict[str, Any] = {
        "id": state.response_id,
        "object": "response",
        "created_at": state.created_at,
        "model": state.model,
        "status": status,
        "output": final_output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "temperature": 1.0,
        "top_p": 1.0,
    }
    if incomplete is not None:
        final_response["incomplete_details"] = incomplete
    if state.usage:
        final_response["usage"] = _translate_usage(state.usage)

    yield _sse_event("response.completed", {
        "sequence_number": state.next_seq(),
        "response": final_response,
    })
