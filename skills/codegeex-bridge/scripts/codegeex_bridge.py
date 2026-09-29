import argparse
import codecs
import json
import os
import re
import time
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field


CODEGEEX_BASE_URL = os.getenv("CODEGEEX_BASE_URL", "https://codegeex.cn/prod/code")
CODEGEEX_AGENT_URL = os.getenv("CODEGEEX_AGENT_URL", "http://localhost:11435")
CODEGEEX_AGENT_MODEL = os.getenv("CODEGEEX_AGENT_MODEL", "codegeex-agent")
TOKEN_URL = os.getenv("CODEGEEX_TOKEN_URL", "http://127.0.0.1:18787/token")
DEFAULT_MODEL = os.getenv("CODEGEEX_MODEL", "glm-5-1")
DEBUG_LAST_PATH = os.getenv("CODEGEEX_DEBUG_LAST_PATH")
DEFAULT_MACHINE_ID = os.getenv(
    "CODEGEEX_MACHINE_ID",
    "34cebee9dcdeeec918155f1ae8ddac16f33577eeaba270b1b5ff5b132014b745",
)
IDE_VERSION = os.getenv("CODEGEEX_IDE_VERSION", "1.122.1")
PLUGIN_VERSION = os.getenv("CODEGEEX_PLUGIN_VERSION", "2.27.6")

# The real CodeGeeX VSCode extension talks to the backend via axios running in
# Node. Captured request (see %TEMP%\liuruyan_codegeex_last_request.json) shows
# no custom UA, which means axios injects its Node defaults. We mirror them so
# the server cannot tell this bridge apart from the genuine extension.
AXIOS_VERSION = os.getenv("CODEGEEX_AXIOS_VERSION", "1.7.3")
AXIOS_DEFAULT_HEADERS = {
    "User-Agent": f"axios/{AXIOS_VERSION}",
    "Accept": "application/json, text/plain, */*",
    "Accept-Encoding": "gzip, compress, deflate, br",
}
# The genuine VSCode extension does not call chatGlmTalk/insert. Worse: inserting
# the full tool-instruction prompt breaks the follow-up chat on the same talkId.
SKIP_INSERT = os.getenv("CODEGEEX_SKIP_INSERT", "1") not in ("0", "false", "False")
MAX_TOOL_SPEC_CHARS = int(os.getenv("CODEGEEX_MAX_TOOL_SPEC_CHARS", "6000"))
MAX_SYSTEM_PROMPT_CHARS = int(os.getenv("CODEGEEX_MAX_SYSTEM_PROMPT_CHARS", "800"))
TOOL_COMPACT_THRESHOLD = int(os.getenv("CODEGEEX_TOOL_COMPACT_THRESHOLD", "12"))
# History budget: the ask channel re-sends the whole conversation every turn, and
# tool results are folded into history verbatim. Without a cap the payload grows
# unbounded across turns and the upstream silently truncates it, which makes the
# model lose the real task and degrade to narration (no tool call). We clamp each
# item, then drop the oldest turns until the total fits the budget.
MAX_HISTORY_CHARS = int(os.getenv("CODEGEEX_MAX_HISTORY_CHARS", "8000"))
MAX_HISTORY_ITEM_CHARS = int(os.getenv("CODEGEEX_MAX_HISTORY_ITEM_CHARS", "2000"))
# Forwarding temperature/max_tokens deviates from the genuine extension body
# schema (which has neither field), so it is OFF by default to keep the request
# indistinguishable from the real client. Enable only if you accept that trade-off.
FORWARD_SAMPLING = os.getenv("CODEGEEX_FORWARD_SAMPLING", "0") not in ("0", "false", "False")
NARRATION_RE = re.compile(r"我来|让我|首先|接下来|正在|准备|开始|尝试|搜索一下")


class ChatMessage(BaseModel):
    role: str
    content: Any = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    model: Optional[str] = None
    messages: List[ChatMessage] = Field(default_factory=list)
    stream: bool = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Any] = None


class CodeGeeXBridge:
    def __init__(self) -> None:
        self._token: Optional[str] = None
        self._user_id: Optional[str] = None
        self._user_role: int = 0
        self._token_checked_at: float = 0.0
        self._agent_session_id: str = str(uuid.uuid4())  # persistent Agent session

    async def get_token(self, force: bool = False) -> str:
        if self._token and not force and time.time() - self._token_checked_at < 60:
            return self._token
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(TOKEN_URL)
            response.raise_for_status()
            data = response.json()
        token = data.get("token") or data.get("code-token") or data.get("code_token")
        if not token:
            raise HTTPException(status_code=502, detail="CodeGeeX token service did not return token")
        self._token = token
        self._token_checked_at = time.time()
        return token

    async def get_user_info(self, force: bool = False) -> Dict[str, Any]:
        token = await self.get_token(force=force)
        if self._user_id and not force:
            return {"user_id": self._user_id, "user_role": self._user_role}
        headers = self._headers(token)
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(f"{CODEGEEX_BASE_URL}/oauth/getUserInfo/", headers=headers)
            response.raise_for_status()
            data = response.json()
        user_data = data.get("data") or data
        user_id = user_data.get("userId") or user_data.get("user_id")
        if not user_id:
            raise HTTPException(status_code=502, detail="CodeGeeX getUserInfo did not return userId")
        self._user_id = user_id
        self._user_role = int(user_data.get("userRole") or user_data.get("user_role") or 0)
        return {"user_id": self._user_id, "user_role": self._user_role}

    async def create_talk(self, prompt: str, talk_id: str) -> None:
        token = await self.get_token()
        headers = self._headers(token)
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                f"{CODEGEEX_BASE_URL}/chatGlmTalk/insert",
                headers=headers,
                json={"talkId": talk_id, "prompt": prompt},
            )
            response.raise_for_status()
            data = response.json()
        if data.get("success") is False or data.get("code") not in (None, 200):
            raise HTTPException(status_code=502, detail={"message": "CodeGeeX create talk failed", "response": data})

    def _chat_body(self, request: ChatCompletionRequest, talk_id: str, prompt: str, user: Dict[str, Any]) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "user_id": user["user_id"],
            "user_role": user["user_role"],
            "ide": "VSCode",
            "ide_version": IDE_VERSION,
            "plugin_version": PLUGIN_VERSION,
            "prompt": prompt,
            "machineId": DEFAULT_MACHINE_ID,
            "talkId": talk_id,
            "locale": "zh",
            "model": request.model or DEFAULT_MODEL,
            "agent": None,
            "candidates": {
                "candidate_msg_id": "",
                "candidate_type": "",
                "selected_candidate": "",
            },
            "history": (
                self._build_history_with_tools(request.messages)
                if request.tools
                else self._build_history(request.messages)
            ),
        }
        # Off by default: only injected when explicitly opted in, since the genuine
        # extension body carries neither field and adding them is a fingerprint.
        if FORWARD_SAMPLING:
            if request.temperature is not None:
                body["temperature"] = request.temperature
            if request.max_tokens is not None:
                body["max_tokens"] = request.max_tokens
        return body

    async def stream_codegeex(self, request: ChatCompletionRequest, talk_id: str, prompt: str) -> AsyncIterator[Dict[str, Any]]:
        user = await self.get_user_info()
        body = self._chat_body(request, talk_id, prompt, user)
        url = f"{CODEGEEX_BASE_URL}/chatCodeSseV3/chat?stream=true"
        async with httpx.AsyncClient(timeout=None) as client:
            # Attempt 0 uses the cached token; on 401 we refresh once and retry the
            # SAME request so a stale token no longer fails the live turn outright.
            for attempt in range(2):
                token = await self.get_token(force=(attempt == 1))
                headers = self._headers(token)
                async with client.stream("POST", url, headers=headers, json=body) as response:
                    response.encoding = "utf-8"
                    if response.status_code == 401 and attempt == 0:
                        await response.aread()
                        continue
                    if response.status_code >= 400:
                        detail = await response.aread()
                        raise HTTPException(
                            status_code=502,
                            detail={
                                "message": "CodeGeeX chat request failed",
                                "status_code": response.status_code,
                                "body": detail.decode("utf-8", errors="replace"),
                            },
                        )
                    event_name = "message"
                    decoder = codecs.getincrementaldecoder("utf-8")()
                    buffer = ""
                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        buffer += decoder.decode(chunk)
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            line = line.rstrip("\r")
                            if not line:
                                continue
                            if line.startswith("event:"):
                                event_name = line.split(":", 1)[1].strip()
                                continue
                            if not line.startswith("data:"):
                                continue
                            raw = line.split(":", 1)[1].strip()
                            if not raw:
                                continue
                            try:
                                payload = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            payload["_event"] = event_name
                            yield payload
                    return

    async def stream_agent(self, request: ChatCompletionRequest, session_id: str) -> AsyncIterator[Dict[str, Any]]:
        """Call CodeGeeX Agent mode via local agent service (port 11435)."""
        token = await self.get_token()
        body = self._agent_body(request, session_id)
        headers = {
            "code-token": token,
            "exp-id": "default",
            "variant-id": "default",
            "is-exp-active": "false",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST",
                f"{CODEGEEX_AGENT_URL}/prod/api/agent",
                headers=headers,
                json=body,
            ) as response:
                response.encoding = "utf-8"
                if response.status_code >= 400:
                    detail = await response.aread()
                    raise HTTPException(
                        status_code=502,
                        detail={
                            "message": "CodeGeeX Agent request failed",
                            "status_code": response.status_code,
                            "body": detail.decode("utf-8", errors="replace"),
                        },
                    )
                event_name = ""
                decoder = codecs.getincrementaldecoder("utf-8")()
                buffer = ""
                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue
                    buffer += decoder.decode(chunk)
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.rstrip("\r")
                        if not line:
                            continue
                        if line.startswith("event:"):
                            event_name = line.split(":", 1)[1].strip()
                            continue
                        if not line.startswith("data:"):
                            continue
                        raw = line.split(":", 1)[1].strip()
                        if not raw:
                            continue
                        try:
                            payload = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        payload["_event"] = event_name
                        yield payload

    # Regex to strip injected <system_instructions> blocks from message text.
    # Once a huge system prompt is injected into the first user message, it stays
    # in the conversation history forever, bloating every subsequent request.
    _SYSTEM_INSTR_RE = re.compile(
        r"<system_instructions>\s*\n.*?\n\s*</system_instructions>\s*\n*", re.DOTALL
    )

    def _agent_body(self, request: ChatCompletionRequest, session_id: str) -> Dict[str, Any]:
        """Build Agent API request body.

        Agent cloud API only reliably handles single-turn requests; multi-turn
        conversations with assistant history cause 400 errors.  We send only the
        LAST user message on each turn.  The Agent service maintains context
        internally via session_id.

        System instructions AND available tool definitions are injected into the
        user message on every turn.  This is critical: the Agent has its own
        built-in VSCode tool set (frontend_server.*, codebase.*, etc.) which do
        NOT exist in the WorkBuddy sandbox.  Without explicit tool injection,
        the Agent will hallucinate tool names and loop endlessly on failures.
        """
        MAX_SYSTEM_CHARS = 2000   # personality / rules
        MAX_TOOL_CHARS = 2000     # tool definitions + sandbox boundary

        system_parts: List[str] = []
        last_user_text = ""

        for msg in request.messages:
            text = _message_text(msg.content)
            if msg.role == "system":
                system_parts.append(text)
            elif msg.role == "user":
                last_user_text = text

        # No user message found — this shouldn't happen
        if not last_user_text:
            last_user_text = "Hello"

        # Build prefix blocks: [system_instructions] + [available_tools] + user message
        prefix_blocks: List[str] = []

        # 1. System instructions (personality + rules)
        if system_parts:
            system_context = "\n\n".join(system_parts)
            if len(system_context) > MAX_SYSTEM_CHARS:
                system_context = system_context[:MAX_SYSTEM_CHARS] + "\n...(系统提示词已截断)"
            prefix_blocks.append(f"<system_instructions>\n{system_context}\n</system_instructions>")

        # 2. Available tools + sandbox boundary
        sandbox_and_tools = _build_sandbox_tool_instruction(request.tools or [])
        if len(sandbox_and_tools) > MAX_TOOL_CHARS:
            sandbox_and_tools = sandbox_and_tools[:MAX_TOOL_CHARS] + "\n...(工具列表已截断)"
        prefix_blocks.append(f"<available_tools>\n{sandbox_and_tools}\n</available_tools>")

        # 3. User message
        prefix_blocks.append(last_user_text)
        full_text = "\n\n".join(prefix_blocks)

        messages = [
            {"role": "user", "content": [{"type": "text", "text": full_text}]}
        ]
        workspace = os.getcwd().replace("\\", "/")

        # Build OpenAI-compatible tools array for Agent native tool support.
        # If the Agent API supports the "tools" field natively, it will use
        # these instead of its built-in VSCode tools.
        agent_tools: List[Dict[str, Any]] = []
        for tool in (request.tools or []):
            func = tool.get("function") or {}
            if func.get("name"):
                agent_tools.append({"type": "function", "function": func})

        body: Dict[str, Any] = {
            "session_id": session_id,
            "model": CODEGEEX_AGENT_MODEL,
            "workspace_names": [workspace],
            "messages": messages,
            "opened_files": [],
            "current_file": "",
            "ide": "VSCode",
            "ide_version": IDE_VERSION,
            "plugin_version": PLUGIN_VERSION,
        }
        if agent_tools:
            body["tools"] = agent_tools
        return body

    def _headers(self, token: str) -> Dict[str, str]:
        # Header set + casing mirrors the captured genuine extension request:
        # code-token / Content-Type / Exp-Id / Variant-Id / Is-Exp-Active,
        # plus axios' own Node defaults (UA / Accept / Accept-Encoding).
        headers = {
            "code-token": token,
            "Content-Type": "application/json",
            "Exp-Id": "",
            "Variant-Id": "",
            "Is-Exp-Active": "false",
        }
        headers.update(AXIOS_DEFAULT_HEADERS)
        return headers

    def _build_history(self, messages: List[ChatMessage]) -> List[Dict[str, Any]]:
        history: List[Dict[str, Any]] = []
        for message in messages[:-1]:
            if message.role not in {"user", "assistant"}:
                continue
            text = _message_text(message.content)
            if not text:
                continue
            history.append({"role": message.role, "content": text})
        return _trim_history(history)

    def _build_history_with_tools(self, messages: List[ChatMessage]) -> List[Dict[str, Any]]:
        """History for tool loops: fold assistant tool-call intents and tool
        results into plain text turns so CodeGeeX sees what it called last turn
        and what came back, then can decide the next step. The final user/tool
        turn is carried by the live prompt, so we stop before the last message.
        """
        history: List[Dict[str, Any]] = []
        for message in messages[:-1]:
            text = _message_text(message.content)
            tool_calls = getattr(message, "tool_calls", None)
            if message.role == "user":
                if text:
                    history.append({"role": "user", "content": text})
            elif message.role == "assistant":
                rendered = text
                if not rendered and tool_calls:
                    rendered = _render_assistant_tool_calls(tool_calls)
                if rendered:
                    history.append({"role": "assistant", "content": rendered})
            elif message.role == "tool":
                if text:
                    history.append({"role": "user", "content": f"工具返回：\n{text}"})
        return _trim_history(history)


def _truncate_text(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + f"\n...(已截断，省略 {len(text) - limit} 字)"


def _trim_history(
    history: List[Dict[str, Any]],
    budget: int = MAX_HISTORY_CHARS,
    item_limit: int = MAX_HISTORY_ITEM_CHARS,
) -> List[Dict[str, Any]]:
    """Clamp each turn, then drop the oldest turns until the total content fits
    the budget. The most recent turn is always kept so the model never loses the
    immediate context (e.g. the latest tool return it must act on)."""
    clamped = [
        {**item, "content": _truncate_text(item.get("content") or "", item_limit)}
        for item in history
    ]
    if budget <= 0 or not clamped:
        return clamped
    total = sum(len(item.get("content") or "") for item in clamped)
    idx = 0
    while total > budget and idx < len(clamped) - 1:
        total -= len(clamped[idx].get("content") or "")
        idx += 1
    return clamped[idx:]


def _build_sandbox_tool_instruction(tools: List[Dict[str, Any]]) -> str:
    """Build sandbox boundary + available tool definitions for the Agent.

    The Agent has its own VSCode tool set; this tells it exactly which tools
    actually exist in the WorkBuddy sandbox, preventing hallucinated tool calls
    and infinite retry loops.
    """
    workspace = os.getcwd().replace("\\", "/")
    home = os.path.expanduser("~").replace("\\", "/")

    lines: List[str] = []
    lines.append("## 沙箱边界 (SANDBOX BOUNDARY)")
    lines.append("")
    lines.append("你运行在一个受限的 WorkBuddy 沙箱环境中，不是 VSCode 环境。")
    lines.append("以下规则必须严格遵守：")
    lines.append("")
    lines.append(f"1. 工作目录：`{workspace}`")
    lines.append(f"2. 你可以读取/写入工作目录及其子目录下的文件")
    lines.append(f"3. 你可以通过绝对路径访问用户主目录 `{home}` 下的文件")
    lines.append("4. 你**不能**访问以下路径：")
    lines.append("   - G: 盘、D: 盘、E: 盘等非系统盘（除非在工作目录内）")
    lines.append("   - C:\\Windows\\、C:\\Program Files\\ 等系统目录")
    lines.append("   - 任何不在工作目录或用户主目录下的路径")
    lines.append("5. 如果用户要求访问外部路径，直接告知无法访问，不要反复尝试")
    lines.append("6. 如果某个工具调用失败，**立即停止**，不要重试同一个工具")
    lines.append("7. 你**没有** frontend_server.* / codebase.* / terminal.* 等 VSCode 工具")

    if tools:
        lines.append("")
        lines.append("## 可用工具 (AVAILABLE TOOLS)")
        lines.append("")
        lines.append("以下是你**唯一**可以调用的工具。不要调用任何未在此列出的工具名：")
        lines.append("")
        for tool in tools:
            func = tool.get("function") or {}
            name = func.get("name")
            if not name:
                continue
            desc = func.get("description", "") or ""
            params = func.get("parameters") or {}
            param_str = json.dumps(params, ensure_ascii=False)
            # Keep each tool entry concise (name + description + params)
            tool_entry = f"### {name}\n描述：{desc}\n参数 schema：{param_str}"
            # Truncate individual tool entry if too long (keep under ~500 chars)
            if len(tool_entry) > 600:
                tool_entry = tool_entry[:600] + "\n...(参数过多已截断)"
            lines.append(tool_entry + "\n")
    else:
        lines.append("")
        lines.append("## 可用工具")
        lines.append("")
        lines.append("当前没有可用的工具。请用纯文本回答用户问题。")
        lines.append("不要尝试调用任何工具——不会有任何工具可用。")

    lines.append("")
    lines.append("## 工具调用规则")
    lines.append("")
    lines.append("1. 只能调用上面列出的工具，不要编造工具名")
    lines.append("2. 工具调用失败后不要重试，改用纯文本说明无法完成")
    lines.append("3. 优先使用纯文本回答简单问题，只在确实需要时调用工具")

    return "\n".join(lines)


bridge = CodeGeeXBridge()
app = FastAPI(title="CodeGeeX OpenAI-compatible Bridge", version="0.1.0")
LAST_DEBUG: Dict[str, Any] = {}


@app.get("/debug/last")
async def debug_last() -> Dict[str, Any]:
    return LAST_DEBUG


@app.get("/health")
async def health() -> Dict[str, Any]:
    token = await bridge.get_token()
    user = await bridge.get_user_info()
    return {
        "ok": True,
        "has_token": bool(token),
        "user_id": user["user_id"],
        "model": DEFAULT_MODEL,
        "agent_model": CODEGEEX_AGENT_MODEL,
        "agent_url": CODEGEEX_AGENT_URL,
    }


@app.get("/v1/models")
async def models() -> Dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {
                "id": DEFAULT_MODEL,
                "object": "model",
                "created": 0,
                "owned_by": "codegeex",
            },
            {
                "id": CODEGEEX_AGENT_MODEL,
                "object": "model",
                "created": 0,
                "owned_by": "codegeex-agent",
            },
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest) -> Any:
    if not request.messages:
        raise HTTPException(status_code=400, detail="messages is required")
    raw_prompt = _last_user_prompt(request.messages)
    if not raw_prompt:
        raise HTTPException(status_code=400, detail="last user message is empty")

    # Route policy: the genuine `chatCodeSseV3` ask channel (glm-5-1) can drive
    # the full tool loop via the <tool_call> text protocol, and it lets us stay
    # in control of execution instead of fighting the extension's built-in agent
    # (which silently drops our tool results). So we force ask mode by default.
    # Set CODEGEEX_FORCE_ASK=0 to fall back to the legacy agent channel.
    force_ask = os.getenv("CODEGEEX_FORCE_ASK", "1") not in ("0", "false", "False")
    use_agent = bool(request.tools) and not force_ask
    session_id = bridge._agent_session_id if use_agent else str(uuid.uuid4())
    _debug_update(
        session_id=session_id,
        stream=request.stream,
        mode="agent" if use_agent else "ask",
        model=request.model or (CODEGEEX_AGENT_MODEL if use_agent else DEFAULT_MODEL),
        has_tools=bool(request.tools),
        tool_names=sorted(_tool_names(request.tools or [])),
        raw_prompt=raw_prompt,
        prompt_len=len(_build_prompt(request, raw_prompt)) if not use_agent else None,
    )

    if use_agent:
        if request.stream:
            return StreamingResponse(
                _openai_stream_agent(request, session_id),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        else:
            return await _agent_non_stream(request, session_id)

    prompt = _build_prompt(request, raw_prompt)
    talk_id = session_id
    if not SKIP_INSERT:
        await bridge.create_talk(prompt, talk_id)
    if request.stream:
        return StreamingResponse(
            _openai_stream(request, talk_id, prompt, raw_prompt),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    content, tool_calls, model = await _complete_ask_with_retry(request, talk_id, prompt, raw_prompt)
    _debug_update(raw_content=content, parsed_tool_calls=tool_calls)
    message: Dict[str, Any] = {"role": "assistant", "content": content}
    finish_reason = "stop"
    if tool_calls:
        message = {"role": "assistant", "content": None, "tool_calls": tool_calls}
        finish_reason = "tool_calls"
    return JSONResponse(
        {
            "id": f"chatcmpl-{session_id}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                }
            ],
        }
    )


async def _complete_ask_once(
    request: ChatCompletionRequest, talk_id: str, prompt: str
) -> tuple[str, List[Dict[str, Any]], str]:
    content = ""
    model = request.model or DEFAULT_MODEL
    async for payload in bridge.stream_codegeex(request, talk_id, prompt):
        if payload.get("text"):
            content = _accumulate_stream_text(content, payload["text"])
        if payload.get("model"):
            model = payload["model"]
        if payload.get("finish_reason"):
            break
    tool_calls = _extract_tool_calls(content, request.tools)
    return content, tool_calls, model


async def _complete_ask_with_retry(
    request: ChatCompletionRequest,
    talk_id: str,
    prompt: str,
    raw_prompt: str,
) -> tuple[str, List[Dict[str, Any]], str]:
    content, tool_calls, model = await _complete_ask_once(request, talk_id, prompt)
    mid_loop = bool(request.messages) and request.messages[-1].role == "tool"
    if not request.tools or tool_calls or not _should_retry_tool(content, raw_prompt, mid_loop):
        return content, tool_calls, model

    retry_prompt = _build_retry_prompt(request, raw_prompt, content)
    retry_talk_id = str(uuid.uuid4())
    retry_content, retry_tool_calls, retry_model = await _complete_ask_once(
        request, retry_talk_id, retry_prompt
    )
    _debug_update(
        retried=True,
        retry_reason="narration_without_tool_call",
        first_raw_content=content,
        raw_content=retry_content or content,
        parsed_tool_calls=retry_tool_calls or tool_calls,
    )
    if retry_tool_calls:
        return retry_content, retry_tool_calls, retry_model
    return content, tool_calls, model


async def _openai_stream(
    request: ChatCompletionRequest, talk_id: str, prompt: str, raw_prompt: str
) -> AsyncIterator[str]:
    model = request.model or DEFAULT_MODEL
    previous = ""
    buffered = ""
    stream_id = f"chatcmpl-{talk_id}"
    created = int(time.time())
    role_chunk = {
        "id": stream_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }
    yield f"data: {json.dumps(role_chunk, ensure_ascii=False)}\n\n"

    if request.tools:
        buffered, tool_calls, model = await _complete_ask_with_retry(
            request, talk_id, prompt, raw_prompt
        )
        _debug_update(raw_content=buffered, parsed_tool_calls=tool_calls)
        finish_reason = "tool_calls" if tool_calls else "stop"
        if tool_calls:
            for index, tool_call in enumerate(tool_calls):
                chunk = {
                    "id": stream_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": index,
                                        "id": tool_call["id"],
                                        "type": "function",
                                        "function": tool_call["function"],
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ],
                }
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        elif buffered:
            chunk = {
                "id": stream_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {"content": buffered}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        final_chunk = {
            "id": stream_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
        }
        yield f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
        return

    async for payload in bridge.stream_codegeex(request, talk_id, prompt):
        text = payload.get("text") or ""
        if payload.get("model"):
            model = payload["model"]
        delta = text[len(previous) :] if text.startswith(previous) else text
        previous = text or previous
        if delta:
            chunk = {
                "id": stream_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        if payload.get("finish_reason"):
            break
    final_chunk = {
        "id": stream_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


async def _openai_stream_agent(request: ChatCompletionRequest, session_id: str) -> AsyncIterator[str]:
    """Stream Agent mode response in OpenAI-compatible SSE format."""
    stream_id = f"chatcmpl-{session_id}"
    created = int(time.time())
    model = request.model or CODEGEEX_AGENT_MODEL

    role_chunk = {
        "id": stream_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }
    yield f"data: {json.dumps(role_chunk, ensure_ascii=False)}\n\n"

    accumulated = ""
    has_tool_call = False
    tool_call_data: Optional[Dict[str, Any]] = None

    async for payload in bridge.stream_agent(request, session_id):
        event = payload.get("_event", "")

        if event == "add":
            text = payload.get("text") or ""
            accumulated += text
            chunk = {
                "id": stream_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

        elif event == "tool_call":
            has_tool_call = True
            meta = payload.get("meta_data") or {}
            tool_info = meta.get("tool") or {}
            tool_call_data = {
                "id": tool_info.get("id", f"call_{uuid.uuid4().hex[:24]}"),
                "type": "function",
                "function": {
                    "name": tool_info.get("name", ""),
                    "arguments": json.dumps(tool_info.get("arguments", {}), ensure_ascii=False),
                },
            }
            chunk = {
                "id": stream_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"tool_calls": [{"index": 0, "id": tool_call_data["id"], "type": "function", "function": tool_call_data["function"]}]},
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

        elif event == "error":
            text = payload.get("text") or "Agent error"
            yield f"data: {json.dumps({'error': text}, ensure_ascii=False)}\n\n"
            break

        elif event == "finish":
            break

    finish_reason = "tool_calls" if has_tool_call else "stop"
    _debug_update(
        raw_content=accumulated,
        parsed_tool_calls=[tool_call_data] if tool_call_data else [],
        has_agent_tool_call=has_tool_call,
    )

    final_chunk = {
        "id": stream_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
    }
    yield f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


async def _agent_non_stream(request: ChatCompletionRequest, session_id: str) -> JSONResponse:
    """Non-streaming Agent mode: accumulate full response and return."""
    accumulated = ""
    tool_call_data: Optional[Dict[str, Any]] = None
    model = request.model or CODEGEEX_AGENT_MODEL

    async for payload in bridge.stream_agent(request, session_id):
        event = payload.get("_event", "")

        if event == "add":
            accumulated += payload.get("text") or ""

        elif event == "tool_call":
            meta = payload.get("meta_data") or {}
            tool_info = meta.get("tool") or {}
            tool_call_data = {
                "id": tool_info.get("id", f"call_{uuid.uuid4().hex[:24]}"),
                "type": "function",
                "function": {
                    "name": tool_info.get("name", ""),
                    "arguments": json.dumps(tool_info.get("arguments", {}), ensure_ascii=False),
                },
            }

        elif event == "error":
            text = payload.get("text") or "Agent error"
            raise HTTPException(status_code=502, detail={"message": "Agent error", "error": text})

        elif event == "finish":
            break

    finish_reason = "tool_calls" if tool_call_data else "stop"
    _debug_update(
        raw_content=accumulated,
        parsed_tool_calls=[tool_call_data] if tool_call_data else [],
    )

    message: Dict[str, Any] = {"role": "assistant", "content": accumulated}
    if tool_call_data:
        message = {"role": "assistant", "content": None, "tool_calls": [tool_call_data]}
        finish_reason = "tool_calls"

    return JSONResponse(
        {
            "id": f"chatcmpl-{session_id}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                }
            ],
        }
    )


def _last_user_prompt(messages: List[ChatMessage]) -> str:
    for message in reversed(messages):
        if message.role == "user":
            return _message_text(message.content).strip()
    return _message_text(messages[-1].content).strip()


def _build_prompt(request: ChatCompletionRequest, user_prompt: str, *, strict: bool = False) -> str:
    if not request.tools:
        tool_messages = _format_tool_messages(request.messages)
        system_text = _collect_system_text(request.messages)
        parts = [p for p in (system_text, tool_messages, user_prompt) if p]
        return "\n\n".join(parts).strip()

    # Tool mode: put the user request BEFORE the tool instruction so the model
    # sees the action target immediately before the format rules (recency bias).
    last = request.messages[-1] if request.messages else None
    system_text = "" if strict else _collect_system_text(request.messages)
    instruction = _tool_instruction(request.tools)
    parts: List[str] = []
    if system_text:
        parts.append(system_text)
    if last is not None and last.role == "tool":
        tool_result = _message_text(last.content)
        parts.append(f"用户任务上下文：\n{tool_result}")
        parts.append(instruction)
        parts.append(
            "请根据以上工具返回结果继续：若信息已足够，直接用纯文本给出最终答复；"
            "若还需要更多信息，再输出一个工具调用。"
        )
    else:
        parts.append(f"用户请求：\n{user_prompt}")
        parts.append(instruction)
    return "\n\n".join(parts).strip()


def _should_retry_tool(content: str, user_prompt: str, mid_loop: bool = False) -> bool:
    stripped = (content or "").strip()
    # Mid tool-loop: the previous turn handed back a tool result and the model is
    # expected to either emit the next tool call or a substantive final answer.
    # A short narration with no tool call means the loop is about to stall, so we
    # force a retry regardless of keyword hints in the original user prompt.
    if mid_loop and "<tool_sep>" not in stripped:
        if not stripped or NARRATION_RE.search(stripped) or len(stripped) < 160:
            return True
    action_hints = ("读", "读取", "查看", "搜索", "运行", "执行", "分析", "打开", "设定", "文件", "代码")
    if not any(hint in user_prompt for hint in action_hints):
        return False
    if not stripped:
        return True
    if NARRATION_RE.search(stripped):
        return True
    return len(stripped) < 160 and "<tool_sep>" not in stripped


def _tool_hint_for_user_request(user_prompt: str, tools: List[Dict[str, Any]]) -> str:
    names = _tool_names(tools)
    if any(keyword in user_prompt for keyword in ("设定", "灵魂", "人设", "性格")):
        if "read_file" in names:
            return '<tool_call>read_file<tool_sep>{"path": "SOUL.md"}</tool_call>'
    if "README" in user_prompt.upper() and "read_file" in names:
        return '<tool_call>read_file<tool_sep>{"path": "README.md"}</tool_call>'
    return ""


def _build_retry_prompt(request: ChatCompletionRequest, user_prompt: str, failed_content: str) -> str:
    names = sorted(_tool_names(request.tools or []))
    hint = _tool_hint_for_user_request(user_prompt, request.tools or [])
    parts = [
        f"用户请求：{user_prompt}",
        "【强制指令】禁止输出任何解释、计划、寒暄。不要使用「我来」「让我」「首先」等词。",
        "你必须只输出一行工具调用，格式：",
        "<tool_call>工具名<tool_sep>{JSON参数}</tool_call>",
        f"可用工具名：{', '.join(names)}",
    ]
    if hint:
        parts.append(f"本次推荐调用：{hint}")
    if failed_content.strip():
        parts.append(f"你刚才的无效回复：{failed_content.strip()[:120]}")
    parts.append("现在立即输出工具调用：")
    return "\n".join(parts)


def _format_tool_messages(messages: List[ChatMessage]) -> str:
    blocks: List[str] = []
    for message in messages:
        if message.role == "tool":
            blocks.append(f"工具返回：\n{_message_text(message.content)}")
    return "\n\n".join(blocks)


def _render_assistant_tool_calls(tool_calls: List[Dict[str, Any]]) -> str:
    """Render OpenAI tool_calls back into the <tool_call> text protocol so a
    prior assistant turn reads consistently to CodeGeeX on the next request."""
    parts: List[str] = []
    for call in tool_calls or []:
        func = (call or {}).get("function") or {}
        name = func.get("name")
        if not name:
            continue
        args = func.get("arguments")
        if isinstance(args, (dict, list)):
            args = json.dumps(args, ensure_ascii=False)
        elif args is None:
            args = "{}"
        parts.append(f"<tool_call>{name}<tool_sep>{args}</tool_call>")
    return "\n".join(parts)


def _collect_system_text(messages: List[ChatMessage]) -> str:
    parts = [_message_text(message.content) for message in messages if message.role == "system"]
    text = "\n\n".join(part.strip() for part in parts if part and part.strip())
    if len(text) > MAX_SYSTEM_PROMPT_CHARS:
        text = text[:MAX_SYSTEM_PROMPT_CHARS] + "\n...(系统提示已截断)"
    return text


def _accumulate_stream_text(previous: str, incoming: str) -> str:
    """Merge SSE text chunks whether the upstream sends cumulative or delta text."""
    if not incoming:
        return previous
    if not previous:
        return incoming
    if incoming.startswith(previous):
        return incoming
    if previous.startswith(incoming):
        return previous
    if previous.endswith(incoming):
        return previous
    return previous + incoming


def _compact_tool_spec(function: Dict[str, Any]) -> Dict[str, Any]:
    name = function.get("name")
    if not name:
        return {}
    description = (function.get("description") or "")[:100]
    parameters = function.get("parameters") or {}
    required = parameters.get("required") or []
    properties = parameters.get("properties") or {}
    compact_props: Dict[str, Any] = {}
    if isinstance(properties, dict):
        for key, value in list(properties.items())[:6]:
            if not isinstance(value, dict):
                continue
            entry: Dict[str, Any] = {"type": value.get("type", "string")}
            if value.get("description"):
                entry["description"] = str(value["description"])[:80]
            compact_props[key] = entry
    return {
        "name": name,
        "description": description,
        "required": required if isinstance(required, list) else [],
        "properties": compact_props,
    }


def _tool_instruction(tools: List[Dict[str, Any]]) -> str:
    names = sorted(_tool_names(tools))
    if len(names) >= TOOL_COMPACT_THRESHOLD:
        read_file_spec = next(
            (_compact_tool_spec(tool.get("function") or {}) for tool in tools if (tool.get("function") or {}).get("name") == "read_file"),
            {},
        )
        return (
            "【工具模式 — 必须遵守】\n"
            "1. 禁止任何解释、计划、寒暄（不要出现「我来」「让我」「首先」）\n"
            "2. 只输出一个工具调用，格式：<tool_call>工具名<tool_sep>{JSON参数}</tool_call>\n"
            "3. 也接受：工具名<tool_sep>{JSON参数}\n"
            f"4. 可用工具：{', '.join(names)}\n"
            f"5. read_file 参数示例：{json.dumps(read_file_spec, ensure_ascii=False)}\n"
            "6. 读设定/人设 → read_file 读 SOUL.md 或 AGENTS.md\n"
            "7. 读文件 → read_file；搜代码 → grep / glob_search；搜网页 → web_search\n"
        )

    specs: List[Dict[str, Any]] = []
    for tool in tools:
        spec = _compact_tool_spec(tool.get("function") or {})
        if spec:
            specs.append(spec)
    schema = json.dumps(specs, ensure_ascii=False)
    if len(schema) > MAX_TOOL_SPEC_CHARS:
        for item in specs:
            item["description"] = (item.get("description") or "")[:40]
            item["properties"] = {}
        schema = json.dumps(specs, ensure_ascii=False)
        if len(schema) > MAX_TOOL_SPEC_CHARS:
            schema = schema[:MAX_TOOL_SPEC_CHARS] + "...(工具列表已截断)"
    return (
        "【工具模式 — 必须遵守】\n"
        "禁止任何解释文字。只输出一个工具调用。\n"
        "格式：<tool_call>工具名<tool_sep>{JSON参数}</tool_call>\n"
        "读设定 → read_file 读 SOUL.md 或 AGENTS.md\n"
        "可用工具 schema：\n"
        f"{schema}"
    )


def _extract_tool_calls(content: str, tools: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    if not tools or not content:
        return []
    allowed_names = _tool_names(tools)
    candidates: List[Dict[str, Any]] = []
    for match in re.finditer(r"<tool_call>\s*([^<\s]+)\s*<tool_sep>\s*(.*?)\s*</tool_call>", content, re.DOTALL):
        candidates.append({"name": match.group(1).strip(), "arguments": match.group(2).strip()})
    if not candidates:
        for allowed_name in allowed_names:
            pattern = re.escape(allowed_name) + r"\s*<tool_sep>\s*(\{.*?\})"
            for match in re.finditer(pattern, content, re.DOTALL):
                candidates.append({"name": allowed_name, "arguments": match.group(1).strip()})
    if not candidates:
        for match in re.finditer(r"(?:^|\n)\s*=?\s*([A-Za-z_][A-Za-z0-9_]*)\s*<tool_sep>\s*(\{.*?\})\s*(?:$|\n)", content, re.DOTALL):
            candidates.append({"name": match.group(1).strip(), "arguments": match.group(2).strip()})
    if not candidates:
        candidates.extend(_json_tool_candidates(content))
    tool_calls = []
    for candidate in candidates:
        name = str(candidate.get("name") or "").strip()
        if not name or name not in allowed_names:
            continue
        arguments = _coerce_arguments(candidate.get("arguments"))
        arguments = _normalize_tool_arguments(name, arguments, tools)
        tool_calls.append(
            {
                "id": f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            }
        )
    return tool_calls


def _json_tool_candidates(content: str) -> List[Dict[str, Any]]:
    stripped = _strip_code_fence(content.strip())
    objects: List[Any] = []
    for text in [stripped, *_balanced_json_objects(stripped)]:
        try:
            objects.append(json.loads(text))
        except json.JSONDecodeError:
            continue
    candidates: List[Dict[str, Any]] = []
    for obj in objects:
        if isinstance(obj, dict) and isinstance(obj.get("tool_calls"), list):
            for item in obj["tool_calls"]:
                if isinstance(item, dict):
                    function = item.get("function") or item
                    candidates.append({"name": function.get("name"), "arguments": function.get("arguments")})
        elif isinstance(obj, dict) and obj.get("name") and "arguments" in obj:
            candidates.append(obj)
    return candidates


def _balanced_json_objects(text: str) -> List[str]:
    chunks: List[str] = []
    depth = 0
    start: Optional[int] = None
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start is not None:
                chunks.append(text[start : index + 1])
                start = None
    return chunks


def _strip_code_fence(text: str) -> str:
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else text


def _coerce_arguments(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _normalize_tool_arguments(name: str, arguments: Dict[str, Any], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
    properties = _tool_properties(name, tools)
    if "path" in properties:
        for alias in ("file_path", "filepath", "filename"):
            if alias in arguments and "path" not in arguments:
                arguments["path"] = arguments.pop(alias)
            elif alias in arguments:
                arguments.pop(alias)
    if properties:
        arguments = {key: value for key, value in arguments.items() if key in properties}
    return arguments


def _tool_names(tools: List[Dict[str, Any]]) -> set[str]:
    names = set()
    for tool in tools:
        function = tool.get("function") or {}
        if function.get("name"):
            names.add(function["name"])
    return names


def _tool_properties(name: str, tools: List[Dict[str, Any]]) -> Dict[str, Any]:
    for tool in tools:
        function = tool.get("function") or {}
        if function.get("name") != name:
            continue
        parameters = function.get("parameters") or {}
        properties = parameters.get("properties") or {}
        return properties if isinstance(properties, dict) else {}
    return {}


def _debug_update(**kwargs: Any) -> None:
    LAST_DEBUG.update(kwargs)
    LAST_DEBUG["updated_at"] = int(time.time())
    if DEBUG_LAST_PATH:
        try:
            with open(DEBUG_LAST_PATH, "w", encoding="utf-8") as f:
                json.dump(LAST_DEBUG, f, ensure_ascii=False, indent=2)
        except OSError:
            pass


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
        return "\n".join(parts)
    return str(content) if content is not None else ""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.getenv("CODEGEEX_BRIDGE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("CODEGEEX_BRIDGE_PORT", "18788")))
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
