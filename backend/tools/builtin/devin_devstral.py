"""Devin Devstral Connect-RPC 客户端（基于 Windsurf / Devin 官方 SWE-grep 协议）。

参考开源实现: https://github.com/SammySnake-d/fast-context-mcp
通过官方云端 Devstral 模型（MODEL_SWE_1_6_FAST）直接驱动多轮语义代码探索子代理：
1. 本地提取或配置 Devin sessionToken；
2. 请求 GetUserJwt 换取 JWT；
3. 发送 query + codebase 紧凑目录树到 GetDevstralStream；
4. Devstral 返回 restricted_exec 指令（rg / readfile / tree / ls / glob）；
5. 本地通过 FastContextSubCommandExecutor 执行并回传结果；
6. 循环推演 1-3 轮，Devstral 返回最终 answer（命中文件与行号区间）。
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import json
import logging
import os
import re
import struct
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
import inspect

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://server.self-serve.windsurf.com/exa.api_server_pb.ApiServerService"
AUTH_BASE = "https://server.self-serve.windsurf.com/exa.auth_pb.AuthService"
WS_APP = "windsurf"
WS_APP_VER = "1.48.2"
WS_LS_VER = "1.9544.35"
WS_MODEL = "MODEL_SWE_1_6_FAST"

FINAL_FORCE_ANSWER = (
    "Provide your final answer now in the specified XML format. "
    "Do NOT call any more commands. Include all relevant files and line ranges found."
)

_SYSTEM_PROMPT_TEMPLATE = """You are a senior code-search agent specializing in semantic code navigation.
Your task is to analyze the user's natural language problem statement and search the provided codebase to identify the precise files and line ranges that contain the relevant implementation or definitions.

Tool Discipline:
- In each round, use `restricted_exec` to run `rg` (ripgrep) or `readfile` commands.
- Only when you have found the exact locations (or after exploring enough context), call the `answer` tool with the XML result.

Answer XML Format:
<answer>
  <file path="path/to/file.ext">
    <range>start-end</range>
    <reason>Why this code is relevant</reason>
  </file>
  <keywords>keyword1, keyword2</keywords>
</answer>
"""


# ============================================================================
# Protobuf / Connect-RPC 极简编码解码工具
# ============================================================================

def _encode_varint(v: int) -> bytes:
    b = bytearray()
    while v > 0x7F:
        b.append((v & 0x7F) | 0x80)
        v >>= 7
    b.append(v & 0x7F)
    return bytes(b)


def _tag(field: int, wire: int) -> bytes:
    return _encode_varint((field << 3) | wire)


def _write_string(field: int, s: str) -> bytes:
    data = s.encode("utf-8")
    return _tag(field, 2) + _encode_varint(len(data)) + data


def _write_bytes(field: int, b: bytes) -> bytes:
    return _tag(field, 2) + _encode_varint(len(b)) + b


def _write_message(field: int, m: bytes) -> bytes:
    return _tag(field, 2) + _encode_varint(len(m)) + m


def _write_varint(field: int, val: int) -> bytes:
    return _tag(field, 0) + _encode_varint(val)


# ============================================================================
# JWT 换取与元数据构建（带内存缓存与长连接复用）
# ============================================================================

_JWT_CACHE: Dict[str, Tuple[str, float]] = {}  # api_key -> (jwt, expiry_timestamp)
_SHARED_CLIENT: Optional[httpx.AsyncClient] = None


def _get_shared_client() -> httpx.AsyncClient:
    global _SHARED_CLIENT
    if _SHARED_CLIENT is None or _SHARED_CLIENT.is_closed:
        _SHARED_CLIENT = httpx.AsyncClient(timeout=35)
    return _SHARED_CLIENT


async def fetch_devin_jwt(api_key: str) -> str:
    """使用 Devin sessionToken / apiKey 从官方鉴权网关换取 JWT（含内存长效缓存，避免每次搜索重复网关往返）。"""
    cleaned_key = api_key.strip()
    now = time.time()
    if cleaned_key in _JWT_CACHE:
        cached_jwt, expiry = _JWT_CACHE[cleaned_key]
        if now < expiry:
            return cached_jwt

    meta = (
        _write_string(1, WS_APP)
        + _write_string(2, WS_APP_VER)
        + _write_string(3, cleaned_key)
        + _write_string(4, "en")
        + _write_string(7, WS_LS_VER)
        + _write_string(12, WS_APP)
        + _write_bytes(30, b"\x00\x01")
    )
    outer = _write_message(1, meta)

    url = f"{AUTH_BASE}/GetUserJwt"
    headers = {
        "Content-Type": "application/proto",
        "Connect-Protocol-Version": "1",
        "User-Agent": "connect-go/1.18.1 (go1.25.5)",
        "Accept-Encoding": "gzip",
    }

    client = _get_shared_client()
    resp = await client.post(url, content=outer, headers=headers)
    if resp.status_code >= 400:
        raise ValueError(f"Devin GetUserJwt HTTP {resp.status_code}: {resp.text[:200]}")
    res_data = resp.content
    # httpx automatically handles decompression; if raw gzip remains, try decompressing
    if res_data.startswith(b"\x1f\x8b"):
        try:
            res_data = gzip.decompress(res_data)
        except Exception:
            pass

    matches = re.findall(rb"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", res_data)
    if not matches:
        raise ValueError("Failed to extract JWT from Devin GetUserJwt response")
    jwt_token = matches[0].decode("utf-8")
    
    # 动态解析 JWT 中的 exp 过期时间戳（通常为 15 分钟），提前 90 秒判定过期
    exp_ts = now + 600.0  # 兜底 10 分钟
    try:
        parts = jwt_token.split(".")
        if len(parts) >= 2:
            p_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
            claims = json.loads(base64.urlsafe_b64decode(p_b64))
            if "exp" in claims:
                exp_ts = float(claims["exp"]) - 90.0
    except Exception:
        pass

    _JWT_CACHE[cleaned_key] = (jwt_token, exp_ts)
    return jwt_token


def _build_metadata(api_key: str, jwt: str) -> bytes:
    sys_info = json.dumps({
        "Os": "win32",
        "Arch": "x64",
        "Release": "10.0",
        "Version": "10.0",
        "Machine": "x64",
        "Nodename": "localhost",
        "Sysname": "Windows_NT",
        "ProductVersion": "",
    })
    cpu_info = json.dumps({
        "NumSockets": 1,
        "NumCores": 8,
        "NumThreads": 8,
        "VendorID": "",
        "Family": "0",
        "Model": "0",
        "ModelName": "CPU",
        "Memory": 16000000000,
    })

    return (
        _write_string(1, WS_APP)
        + _write_string(2, WS_APP_VER)
        + _write_string(3, api_key.strip())
        + _write_string(4, "en")
        + _write_string(5, sys_info)
        + _write_string(7, WS_LS_VER)
        + _write_string(8, cpu_info)
        + _write_string(12, WS_APP)
        + _write_string(21, jwt)
        + _write_bytes(30, b"\x00\x01")
    )


def _get_tool_definitions(max_commands: int = 8) -> str:
    props = {}
    for i in range(1, max_commands + 1):
        props[f"command{i}"] = {
            "type": "object",
            "description": f"Command {i} to execute.",
            "properties": {
                "type": {"type": "string", "enum": ["rg", "readfile", "tree", "ls", "glob"]},
                "pattern": {"type": "string"},
                "path": {"type": "string"},
                "file": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            },
            "required": ["type"],
        }
    tools = [
        {
            "type": "function",
            "function": {
                "name": "restricted_exec",
                "description": "Execute restricted commands in parallel.",
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": ["command1"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "answer",
                "description": "Final answer with relevant files and line ranges.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "answer": {"type": "string", "description": "XML formatted answer"},
                    },
                    "required": ["answer"],
                },
            },
        },
    ]
    return json.dumps(tools)


def _build_request_body(
    api_key: str,
    jwt: str,
    messages: List[Dict[str, Any]],
    tool_defs: str,
) -> bytes:
    meta_bytes = _build_metadata(api_key, jwt)
    parts = [_write_message(1, meta_bytes)]

    for m in messages:
        role = m.get("role", 1)
        content = m.get("content", "")
        msg_parts = [
            _write_varint(2, role),
            _write_string(3, content),
        ]
        if m.get("tool_call_id") and m.get("tool_name") and m.get("tool_args_json"):
            tc = (
                _write_string(1, m["tool_call_id"])
                + _write_string(2, m["tool_name"])
                + _write_string(3, m["tool_args_json"])
            )
            msg_parts.append(_write_message(6, tc))
        if m.get("ref_call_id"):
            msg_parts.append(_write_string(7, m["ref_call_id"]))

        parts.append(_write_message(2, b"".join(msg_parts)))

    parts.append(_write_string(3, tool_defs))
    full_req = b"".join(parts)

    gzipped = gzip.compress(full_req)
    header = struct.pack(">BI", 1, len(gzipped))
    return header + gzipped


def _clean_thought(text: str) -> str:
    """清理 protobuf 报文残留的二进制控制字符（如 \x12\x04 等豆腐块）与空白。"""
    if not text:
        return ""
    # 过滤 C0/C1 控制字符（保留 \n 和 \t）以及 Unicode 替换符 \ufffd
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ufffd]", "", text)
    # 过滤方块符号与豆腐块非打印字符
    cleaned = re.sub(r"[\u25a0-\u25ff\u2b12-\u2b1b]", "", cleaned)
    cleaned = cleaned.strip()
    # 若无有效文字（如只有残余符号标点），视为空思考避免幽灵轮
    if len(re.sub(r"[^a-zA-Z0-9\u4e00-\u9fa5]", "", cleaned)) < 2:
        return ""
    return cleaned


def _parse_response_frames(stream_data: bytes) -> Tuple[str, Optional[Tuple[str, Dict[str, Any]]]]:
    idx = 0
    all_text = ""
    while idx + 5 <= len(stream_data):
        flags, flen = struct.unpack(">BI", stream_data[idx:idx + 5])
        idx += 5
        payload = stream_data[idx:idx + flen]
        idx += flen
        if flags & 1:
            try:
                payload = gzip.decompress(payload)
            except Exception:
                pass
        text = payload.decode("utf-8", errors="ignore")
        if "[TOOL_CALLS]" in text:
            all_text = text
            break
        # 提取较长可读字符串
        for part in re.findall(r"[\x20-\x7E\u4e00-\u9fa5]{10,}", text):
            all_text += part + "\n"

    # 解析 [TOOL_CALLS]name[ARGS]{...} 或 [TOOL_CALLS]name {...}
    cleaned = all_text.replace("</s>", "")
    m = re.search(r"\[TOOL_CALLS\](\w+)(?:\[ARGS\]|\s*)(\{.+)", cleaned, re.DOTALL)
    if not m:
        # 如果是 Connect 错误报文（如 {"error":{"code":"unauthenticated"...}}），绝不能当成模型思考
        if '"error":' in cleaned and '"code":' in cleaned:
            return "", None
        return _clean_thought(cleaned), None

    tool_name = m.group(1)
    raw_json = m.group(2).strip()
    # 找到匹配的大括号
    depth = 0
    end = 0
    for i, ch in enumerate(raw_json):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end == 0:
        end = len(raw_json)

    json_text = raw_json[:end]
    try:
        args = json.loads(json_text)
    except Exception:
        args = {}

    thought = _clean_thought(cleaned[:m.start()])
    return thought, (tool_name, args)


# ============================================================================
# Devin Devstral 核心搜索执行循环
# ============================================================================

async def run_devin_devstral_search(
    query: str,
    project_root: str,
    api_key: str,
    max_turns: int = 3,
    on_thought: Optional[Callable[[int, str, float], Any]] = None,
    on_step: Optional[Callable[[Dict[str, Any]], Any]] = None,
    on_round_done: Optional[Callable[[int], Any]] = None,
) -> Dict[str, Any]:
    """通过 Devin 官方 Devstral 搜索子代理进行多轮推演检索。"""
    from .fast_context_tool import FastContextSubCommandExecutor, _build_compact_tree, _parse_xml_commands

    root_path = Path(project_root).resolve()
    executor = FastContextSubCommandExecutor(root_path)

    # 1. 换取 JWT
    jwt = await fetch_devin_jwt(api_key)

    # 2. 准备代码库树与提示词
    compact_tree = _build_compact_tree(root_path, target_depth=3)
    user_content = f"Problem Statement: {query}\n\nRepo Map (tree -L 3 /codebase):\n```text\n{compact_tree}\n```"

    messages = [
        {"role": 5, "content": _SYSTEM_PROMPT_TEMPLATE},
        {"role": 1, "content": user_content},
    ]

    tool_defs = _get_tool_definitions(max_commands=6)
    all_steps: List[Dict[str, Any]] = []
    all_rounds: List[Dict[str, Any]] = []
    final_results: List[Dict[str, Any]] = []
    final_keywords: List[str] = []
    summary_text = ""

    client = _get_shared_client()
    if True:
        for turn in range(1, max_turns + 2):
            t_turn_start = time.perf_counter()
            body = _build_request_body(api_key, jwt, messages, tool_defs)

            trace_id = uuid.uuid4().hex
            span_id = trace_id[:16]
            headers = {
                "Content-Type": "application/connect+proto",
                "Connect-Protocol-Version": "1",
                "Connect-Accept-Encoding": "gzip",
                "Connect-Content-Encoding": "gzip",
                "Connect-Timeout-Ms": "30000",
                "User-Agent": "connect-go/1.18.1 (go1.25.5)",
                "Accept-Encoding": "identity",
                "Baggage": f"sentry-release=language-server-windsurf@{WS_LS_VER},sentry-environment=stable,sentry-sampled=false,sentry-trace_id={trace_id},sentry-public_key=b813f73488da69eedec534dba1029111",
                "Sentry-Trace": f"{trace_id}-{span_id}-0",
            }

            resp = await client.post(f"{API_BASE}/GetDevstralStream", content=body, headers=headers)

            # 若遇到 token 过期 (401/unauthenticated)，立即清空 JWT 缓存、自动重新换取最新 JWT 并重试一次本轮
            if resp.status_code in (401, 403) or b'"unauthenticated"' in resp.content:
                logger.warning(f"[Devstral] Received unauthenticated (HTTP {resp.status_code}), refreshing JWT and retrying...")
                _JWT_CACHE.pop(api_key.strip(), None)
                try:
                    jwt = await fetch_devin_jwt(api_key)
                    body = _build_request_body(api_key, jwt, messages, tool_defs)
                    resp = await client.post(f"{API_BASE}/GetDevstralStream", content=body, headers=headers)
                except Exception as ex:
                    logger.error(f"[Devstral] Failed to auto-refresh JWT: {ex}")

            if resp.status_code >= 400:
                logger.warning(f"[Devstral] Stream request failed HTTP {resp.status_code}: {resp.text[:200]}")
                break

            thought, tool_info = _parse_response_frames(resp.content)
            thought_dur = time.perf_counter() - t_turn_start

            tool_name = tool_info[0] if tool_info else None
            tool_args = tool_info[1] if tool_info else {}

            # 仅在非 answer 终结调用、且包含有效推演思考文本时，才推送 on_thought 避免出现 0 步幽灵轮
            current_round_num = len(all_rounds) + 1
            if tool_name != "answer" and on_thought and thought:
                await on_thought(current_round_num, thought, round(thought_dur, 2))

            round_steps: List[Dict[str, Any]] = []

            if not tool_info:
                # 检查是否存在文本中的 xml answer
                _, _, answer_dict = _parse_xml_commands(thought)
                if answer_dict and answer_dict.get("files"):
                    final_results = answer_dict["files"]
                    final_keywords = answer_dict.get("keywords", [])
                break

            if tool_name == "answer":
                answer_xml = tool_args.get("answer", "")
                _, _, answer_dict = _parse_xml_commands(answer_xml)
                if answer_dict and answer_dict.get("files"):
                    final_results = answer_dict["files"]
                    final_keywords = answer_dict.get("keywords", [])
                break

            if tool_name == "restricted_exec":
                call_id = str(uuid.uuid4())
                args_json = json.dumps(tool_args)

                # 解析各 command 并本地执行
                exec_results = []
                for k in sorted(tool_args.keys()):
                    if not k.startswith("command"):
                        continue
                    cmd = tool_args[k]
                    ctype = cmd.get("type", "")
                    if ctype == "rg":
                        pat = cmd.get("pattern", "")
                        p = cmd.get("path", ".")
                        out, dur = executor.run_rg(pat, path=p)
                        step_item = {
                            "action": "Grepped",
                            "command": f'rg "{pat}" in {p}',
                            "duration": dur,
                            "round": current_round_num,
                        }
                        round_steps.append(step_item)
                        all_steps.append(step_item)
                        if on_step:
                            await on_step(step_item)
                        exec_results.append(f"Command {k} (rg {pat}):\n{out}\n")
                    elif ctype == "readfile":
                        fpath = cmd.get("file", "")
                        s_line = cmd.get("start_line", 1)
                        e_line = cmd.get("end_line", 100)
                        out, dur = executor.run_readfile(fpath, s_line, e_line)
                        step_item = {
                            "action": "Read",
                            "command": f"read {fpath} (L{s_line}-{e_line})",
                            "duration": dur,
                            "round": current_round_num,
                        }
                        round_steps.append(step_item)
                        all_steps.append(step_item)
                        if on_step:
                            await on_step(step_item)
                        exec_results.append(f"Command {k} (read {fpath}):\n{out}\n")
                    elif ctype == "tree":
                        p = cmd.get("path", ".")
                        out, dur = executor.run_tree(p)
                        step_item = {
                            "action": "Tree",
                            "command": f"tree {p}",
                            "duration": dur,
                            "round": current_round_num,
                        }
                        round_steps.append(step_item)
                        all_steps.append(step_item)
                        if on_step:
                            await on_step(step_item)
                        exec_results.append(f"Command {k} (tree {p}):\n{out}\n")
                    elif ctype in ("glob", "ls"):
                        p = cmd.get("path", ".")
                        pat = cmd.get("pattern", "*")
                        t0 = time.perf_counter()
                        res_list = []
                        resolved_p = executor._safe_resolve(p)
                        if resolved_p and resolved_p.exists():
                            if resolved_p.is_dir():
                                try:
                                    items = list(resolved_p.glob(pat if ctype == "glob" else "*"))[:40]
                                    for item in items:
                                        rel = item.relative_to(executor.root).as_posix()
                                        res_list.append(f"/codebase/{rel}")
                                except Exception:
                                    pass
                            else:
                                res_list.append(f"/codebase/{resolved_p.relative_to(executor.root).as_posix()}")
                        dur = round(time.perf_counter() - t0, 3)
                        out_text = "\n".join(res_list) if res_list else "No files found."
                        action_name = "Globbed" if ctype == "glob" else "Listed"
                        cmd_display = f"{ctype} {pat} in {p}" if ctype == "glob" else f"ls {p}"
                        step_item = {
                            "action": action_name,
                            "command": cmd_display,
                            "duration": dur,
                            "round": current_round_num,
                        }
                        round_steps.append(step_item)
                        all_steps.append(step_item)
                        if on_step:
                            await on_step(step_item)
                        exec_results.append(f"Command {k} ({cmd_display}):\n{out_text}\n")
                    else:
                        exec_results.append(f"Command {k} ({ctype}): skipped\n")

                if round_steps or (thought and len(re.sub(r"[^a-zA-Z0-9\u4e00-\u9fa5]", "", thought)) >= 2):
                    all_rounds.append({
                        "round": current_round_num,
                        "thought": thought,
                        "thought_duration": round(thought_dur, 2),
                        "steps": round_steps,
                        "isCompleted": True,
                    })

                if on_round_done:
                    res = on_round_done(current_round_num)
                    if inspect.iscoroutine(res) or asyncio.iscoroutine(res):
                        await res

                messages.append({
                    "role": 2,
                    "content": thought,
                    "tool_call_id": call_id,
                    "tool_name": "restricted_exec",
                    "tool_args_json": args_json,
                })
                messages.append({
                    "role": 4,
                    "content": "\n".join(exec_results),
                    "ref_call_id": call_id,
                })

                # 给前端 UI 预留自然的步进节奏缓冲 (250ms)，避免云端多轮快速推演瞬间硬切
                await asyncio.sleep(0.25)

                # 若到达最后一轮，强制注入收尾
                if turn >= max_turns:
                    messages.append({"role": 1, "content": FINAL_FORCE_ANSWER})

    # 规范化 results 行号和路径（去除 /codebase 虚拟前缀）
    for r in final_results:
        fpath = r.get("path", "")
        if fpath.startswith("/codebase/"):
            fpath = fpath[len("/codebase/"):]
        elif fpath.startswith("/codebase"):
            fpath = fpath[len("/codebase"):].lstrip("/\\")
        r["path"] = fpath
        fpath_resolved = (root_path / fpath).resolve()
        r["full_path"] = str(fpath_resolved)
        r["exists"] = fpath_resolved.exists()

    if final_results:
        file_lines = [f"- {r['path']} (L{r.get('ranges', [[1, 1]])[0][0]}-{r.get('ranges', [[1, 1]])[0][1]}): {r.get('reason', '')}" for r in final_results]
        summary_text = f"Devin Devstral 找到 {len(final_results)} 处关键代码位置：\n" + "\n".join(file_lines)
    else:
        summary_text = "Devin Devstral 未能在当前轮数内定位到确切代码匹配。"

    return {
        "success": True,
        "engine": "devin_devstral",
        "results": final_results,
        "keywords": final_keywords,
        "summary": summary_text,
        "steps": all_steps,
        "rounds": all_rounds,
        "total_duration": 0,
        "_meta": {
            "query": query,
            "engine": "devin_devstral",
            "rounds_count": len(all_rounds),
            "results_count": len(final_results),
            "results": final_results,
            "keywords": final_keywords,
            "steps": all_steps,
            "rounds": all_rounds,
            "summary": summary_text,
            "total_duration": 0,
        },
    }
