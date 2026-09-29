"""上下文体检：拆解一条真实会话送进模型时各部分占多少 token。

目的（回应"我总感觉我们的会话很容易爆"）：
    柳如烟只在 history 层用 50k 阈值触发压缩，但真正进模型的是
    系统提示 + 记忆上下文 + 工具 schema + 历史 + 本轮工具结果 全叠加。
    本脚本把这几坨分别称重，对照模型真实窗口，定位到底是哪坨撑爆的。

只读，不改任何状态。用系统自己的 estimate_tokens（和 overflow 50k 阈值同一把尺）。

用法：
    cd backend
    python _context_audit.py                      # 自动挑最大的 transcript
    python _context_audit.py <transcript.jsonl 路径>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from backend.memory_manager import (
    CONVERSATION_MAX_TOKENS,
    CONVERSATION_WARNING_TOKENS,
    estimate_tokens,
)
from backend.transcript import TranscriptReader
from backend.tools import get_tool_registry

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _fmt(n: int) -> str:
    return f"{n:,}"


def _bar(tokens: int, total: int, width: int = 40) -> str:
    if total <= 0:
        return ""
    filled = int(round(width * tokens / total))
    return "█" * filled + "·" * (width - filled)


def _content_to_text(content) -> str:
    """把 message content（str 或 list[block]）拍平成可估算的文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(json.dumps(block, ensure_ascii=False))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return str(content)


def _pick_largest_transcript() -> Path | None:
    candidates = list(PROJECT_ROOT.rglob("transcript.jsonl"))
    candidates = [c for c in candidates if "sessions.backup" not in str(c)]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_size)


def _infer_agent_id(transcript_path: Path) -> str | None:
    """从路径 agents/<agent_id>/sessions/<sid>/transcript.jsonl 反推 agent_id。"""
    parts = transcript_path.parts
    if "agents" in parts:
        i = parts.index("agents")
        if i + 1 < len(parts):
            return parts[i + 1]
    return None


def audit(transcript_path: Path) -> None:
    print("=" * 72)
    print(f"上下文体检：{transcript_path}")
    print(f"文件大小：{_fmt(transcript_path.stat().st_size)} bytes")
    print("=" * 72)

    reader = TranscriptReader(transcript_path)
    history = reader.rebuild_conversation_history()
    print(f"\n重建对话历史：{len(history)} 条消息\n")

    # ── 1. 历史按角色拆解 ────────────────────────────────────────────
    role_tokens: dict[str, int] = {}
    role_counts: dict[str, int] = {}
    per_msg: list[tuple[int, str, str]] = []  # (tokens, role, preview)
    for m in history:
        role = m.get("role", "?")
        text = _content_to_text(m.get("content"))
        tok = estimate_tokens(text)
        role_tokens[role] = role_tokens.get(role, 0) + tok
        role_counts[role] = role_counts.get(role, 0) + 1
        preview = text.replace("\n", " ")[:60]
        per_msg.append((tok, role, preview))

    history_total = sum(role_tokens.values())

    # ── 2. 工具 schema（每次请求都发，固定大头）────────────────────
    registry = get_tool_registry()
    tools_openai = registry.get_tool_definitions(format="openai")
    tools_json = json.dumps(tools_openai, ensure_ascii=False)
    tools_tokens = estimate_tokens(tools_json)
    tools_count = len(tools_openai)

    # ── 3. 记忆上下文（best-effort）────────────────────────────────
    memory_tokens = 0
    memory_note = ""
    agent_id = _infer_agent_id(transcript_path)
    if agent_id:
        try:
            from backend.memory_manager import get_memory_manager

            mm = get_memory_manager(agent_id=agent_id)
            mem_ctx = mm.get_context_for_chat()
            memory_tokens = estimate_tokens(mem_ctx or "")
            memory_note = f"(agent_id={agent_id})"
        except Exception as exc:  # noqa: BLE001
            memory_note = f"(取记忆失败: {exc})"
    else:
        memory_note = "(无法从路径推断 agent_id)"

    # ── 汇总 ───────────────────────────────────────────────────────
    grand_total = history_total + tools_tokens + memory_tokens
    print("【各部分 token 占用（估算，与 overflow 50k 同一把尺）】\n")
    rows = [
        ("对话历史 history", history_total),
        ("工具 schema（每请求必发）", tools_tokens),
        (f"记忆上下文 {memory_note}", memory_tokens),
    ]
    for label, tok in rows:
        pct = (tok / grand_total * 100) if grand_total else 0
        print(f"  {label:<34} {_fmt(tok):>10} tok  {pct:5.1f}%  {_bar(tok, grand_total)}")
    print(f"  {'─' * 34} {'─' * 10}")
    print(f"  {'合计（未含系统提示/人格/本轮工具结果）':<34} {_fmt(grand_total):>10} tok")

    # ── 历史角色细分 ───────────────────────────────────────────────
    print("\n【对话历史按角色细分】\n")
    for role in sorted(role_tokens, key=lambda r: -role_tokens[role]):
        print(
            f"  {role:<12} {role_counts[role]:>4} 条  "
            f"{_fmt(role_tokens[role]):>10} tok  "
            f"{role_tokens[role] / history_total * 100 if history_total else 0:5.1f}%"
        )

    # ── 最大的几条单条消息（单条爆点）─────────────────────────────
    print("\n【单条消息 top 8（最可能是单工具结果撑爆）】\n")
    for tok, role, preview in sorted(per_msg, reverse=True)[:8]:
        print(f"  {_fmt(tok):>9} tok  [{role:<9}] {preview}")

    # ── 对照模型窗口 + 阈值 ───────────────────────────────────────
    print("\n【对照】\n")
    window = 128_000  # 当前 deepseek-v4-flash:free 窗口
    print(f"  模型窗口（deepseek-v4-flash）         {_fmt(window):>10} tok")
    print(f"  history 压缩阈值 overflow            {_fmt(CONVERSATION_MAX_TOKENS):>10} tok")
    print(f"  history 压缩阈值 warning             {_fmt(CONVERSATION_WARNING_TOKENS):>10} tok")
    print(f"  实测 history                          {_fmt(history_total):>10} tok"
          f"  {'← 已超 overflow，会触发压缩' if history_total > CONVERSATION_MAX_TOKENS else ''}")
    print(f"  实测 合计(史+工具+记忆)               {_fmt(grand_total):>10} tok"
          f"  {'← 占窗口 %.0f%%' % (grand_total / window * 100)}")
    print("\n  注：合计还没算 系统提示 + 人格 + 本轮新产生的工具结果，")
    print("      真实送入量比这更高。若 history 压缩后仍叠上 ~%s tok 的工具 schema，" % _fmt(tools_tokens))
    print("      就是'压了历史也照样逼近窗口'的根因之一。")
    print("=" * 72)


def main() -> None:
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        path = _pick_largest_transcript()
        if path is None:
            print("未找到任何 transcript.jsonl")
            sys.exit(1)
        print(f"未指定路径，自动挑选最大的 transcript：{path}\n")
    if not path.exists():
        print(f"路径不存在：{path}")
        sys.exit(1)
    audit(path)


if __name__ == "__main__":
    main()
