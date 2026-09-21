"""把 v1 segments JSON string 格式的 transcript 迁移到 v3 Anthropic content blocks。

v1 长这样：
    {"type":"message","role":"assistant","content":"{\\"content\\":..., \\"segments\\":[...]}", ...}
v3 长这样：
    {"type":"message","role":"assistant","content":[{"type":"text","text":"..."}, {"type":"tool_use",...}, ...], ...}
    {"type":"message","role":"user","content":[{"type":"tool_result","tool_use_id":"...","content":"..."}, ...], ...}

只迁 v1：
- v2 entries (assistant content=str + 顶层 tool_calls 数组 / role=tool 独立 entry)
  跳过不动。v2 写盘时已把 text/tool_use 交错信息拍扁，迁移再多遍也回不来。
  Phase E (2026-05-20) 后 read 侧也已不再兼容 v2 残留——本脚本只负责 v1→v3；
  若磁盘上确有 v2 残留（盘上扫描应为 0 条），需用户手动处理或从备份恢复。
- v3 entries (content 是 list) 跳过不动，已经是目标格式。
- 其他 type=emotion/mask/worker 等非 message 行原样保留。

用法：
    python -m backend.scripts.migrate_transcripts_v3                     # dry-run 报告
    python -m backend.scripts.migrate_transcripts_v3 --apply             # 真改 + .bak 备份
    python -m backend.scripts.migrate_transcripts_v3 --apply --no-backup # 真改不留备份（小心）
    python -m backend.scripts.migrate_transcripts_v3 --root <path>       # 指定 agents 目录

安全：
- 默认 dry-run
- 写盘前 .bak 备份（除非 --no-backup）
- 原子替换（先写 .new 再 rename）
- 损坏行原样保留（不阻断）
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def _segments_to_v3_blocks(
    segments: List[Dict],
    fallback_text: str,
) -> Tuple[List[Dict], List[Dict]]:
    """把 v1 segments 数组拆成 v3 (assistant_blocks, tool_results)。

    与 backend.main._segments_to_anthropic_blocks 同语义，独立实现避免依赖
    backend.main 的整个 FastAPI import 链。

    Returns:
        (assistant_blocks, tool_results)
        - assistant_blocks: thinking/text/tool_use blocks，按 segments 原顺序
        - tool_results: list of {tool_use_id, content}，调用方拼成 user 消息
    """
    assistant_blocks: List[Dict] = []
    tool_results: List[Dict] = []

    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        stype = seg.get("type")
        if stype == "thinking":
            text = seg.get("content", "") or ""
            if text:
                assistant_blocks.append({"type": "thinking", "thinking": text})
        elif stype == "text":
            text = seg.get("content", "") or ""
            if text:
                assistant_blocks.append({"type": "text", "text": text})
        elif stype == "tool_call":
            tool_use_id = seg.get("tool_use_id") or seg.get("id") or ""
            name = seg.get("tool_name") or seg.get("name") or ""
            args = seg.get("input")
            if args is None:
                args = seg.get("arguments")
            # Anthropic input 必须是 dict
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except (json.JSONDecodeError, ValueError):
                    args = {}
            elif not isinstance(args, dict):
                args = {}
            if tool_use_id and name:
                assistant_blocks.append({
                    "type": "tool_use",
                    "id": str(tool_use_id),
                    "name": str(name),
                    "input": args,
                })
        elif stype == "tool_result":
            tool_use_id = seg.get("tool_use_id") or ""
            if not tool_use_id:
                continue
            content = seg.get("content")
            if content is None:
                content = seg.get("result", "") or ""
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": str(tool_use_id),
                "content": content,
            })
        # system_notice / 其他类型不映射，丢失（v3 LLM 协议不支持柳如烟自有概念）

    # 兜底：assistant_blocks 完全空但有 fallback_text → 至少留个 text block
    if not assistant_blocks and fallback_text:
        assistant_blocks.append({"type": "text", "text": fallback_text})

    return assistant_blocks, tool_results


def _is_v1_assistant_entry(entry: Dict) -> bool:
    """判断是否 v1 segments JSON string 格式的 assistant entry。"""
    if entry.get("type") != "message":
        return False
    if entry.get("role") != "assistant":
        return False
    # v3：content 已经是 list
    if isinstance(entry.get("content"), list):
        return False
    # v2：顶层有 tool_calls 或 reasoning_content
    if entry.get("tool_calls") or entry.get("reasoning_content"):
        return False
    content = entry.get("content", "")
    if not isinstance(content, str):
        return False
    s = content.strip()
    if not s.startswith("{"):
        return False
    # 最后判断：能不能解出 segments
    try:
        parsed = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return False
    return isinstance(parsed, dict) and isinstance(parsed.get("segments"), list)


def _migrate_v1_assistant(entry: Dict) -> Optional[List[Dict]]:
    """把一条 v1 assistant entry 迁移成 v3 entry 序列。

    Returns:
        - None: 不是 v1 entry，调用方原样保留
        - List[Dict]: 1+1 条 v3 entry（assistant blocks + 可选 user tool_result）
    """
    if not _is_v1_assistant_entry(entry):
        return None

    try:
        parsed = json.loads(entry["content"])
    except (json.JSONDecodeError, ValueError):
        return None

    segments = parsed.get("segments", [])
    fallback_text = parsed.get("content", "") or ""
    assistant_blocks, tool_results = _segments_to_v3_blocks(segments, fallback_text)

    # 完全空内容 → 保留为 v3 空 assistant（不抛弃这条 entry，保持时间线连续性）
    if not assistant_blocks:
        assistant_blocks = [{"type": "text", "text": fallback_text or "[已处理]"}]

    out: List[Dict] = []

    new_assistant: Dict = {
        "type": "message",
        "role": "assistant",
        "content": assistant_blocks,
        "timestamp": entry.get("timestamp"),
    }
    for k in ("client_id", "agent_id"):
        if entry.get(k):
            new_assistant[k] = entry[k]
    out.append(new_assistant)

    # tool_results 合成下一条 user 消息（Anthropic 协议要求）
    if tool_results:
        out.append({
            "type": "message",
            "role": "user",
            "content": tool_results,
            "timestamp": entry.get("timestamp"),  # 同 assistant 时间戳
        })

    return out


# ──────────────────────────── 文件级迁移 ────────────────────────────


def migrate_file(path: Path) -> Tuple[int, int, List[str], List[str]]:
    """迁移单个 transcript.jsonl 到 v3。

    Returns:
        (v1_migrated, total_lines, broken_lines, new_jsonl_lines)
    """
    if not path.exists():
        return 0, 0, [], []

    new_lines: List[str] = []
    v1_migrated = 0
    total_lines = 0
    broken: List[str] = []

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            total_lines += 1
            try:
                entry = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                new_lines.append(stripped)
                broken.append(stripped[:120])
                continue

            migrated = _migrate_v1_assistant(entry)
            if migrated is None:
                # v2 / v3 / non-message / user / system / 等等 — 原样保留
                new_lines.append(json.dumps(entry, ensure_ascii=False))
            else:
                v1_migrated += 1
                for new_entry in migrated:
                    new_lines.append(json.dumps(new_entry, ensure_ascii=False))

    return v1_migrated, total_lines, broken, new_lines


def _atomic_write(path: Path, lines: List[str], make_backup: bool) -> None:
    """先写 .new 再 rename，可选 .bak 备份。"""
    if make_backup:
        backup = path.with_suffix(path.suffix + ".bak")
        shutil.copyfile(path, backup)

    new_path = path.with_suffix(path.suffix + ".new")
    with open(new_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
        if lines:
            f.write("\n")
    new_path.replace(path)


def discover_transcripts(root: Path) -> List[Path]:
    if not root.exists():
        return []
    return sorted(root.glob("*/sessions/*/transcript.jsonl"))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="把 transcript.jsonl 从 v1 (segments JSON string) 迁移到 v3 (Anthropic content blocks)"
    )
    parser.add_argument("--root", type=str, default=None, help="agents 根目录，默认 paths.agents_dir()")
    parser.add_argument("--apply", action="store_true", help="真改写（默认只 dry-run 报告）")
    parser.add_argument("--no-backup", action="store_true", help="改写时不留 .bak 备份")
    args = parser.parse_args(argv)

    if args.root:
        root = Path(args.root)
    else:
        from backend import paths
        root = paths.agents_dir()

    files = discover_transcripts(root)
    print(f"扫描 {root}：发现 {len(files)} 个 transcript.jsonl")
    if not files:
        return 0

    total_migrated = 0
    total_broken = 0
    total_lines = 0
    affected_files: List[Tuple[Path, int]] = []

    for f in files:
        migrated, lines, broken, new_lines = migrate_file(f)
        total_migrated += migrated
        total_broken += len(broken)
        total_lines += lines
        if migrated > 0:
            affected_files.append((f, migrated))
            if args.apply:
                _atomic_write(f, new_lines, make_backup=not args.no_backup)

    print(f"扫描行数：{total_lines}")
    print(f"v1 -> v3 待迁移 entries: {total_migrated}（{len(affected_files)} 个文件受影响）")
    if total_broken:
        print(f"⚠ 损坏行（保留原样）：{total_broken}")
    print()
    print("受影响文件清单（前 20 个）：")
    for f, n in affected_files[:20]:
        print(f"  {f.relative_to(root)}: {n} 条")
    if len(affected_files) > 20:
        print(f"  ...（还有 {len(affected_files) - 20} 个）")

    print()
    if not args.apply:
        print("-> dry-run 完成。加 --apply 实际改写。")
    else:
        msg = "已留 .bak 备份" if not args.no_backup else "未留备份，小心"
        print(f"[OK] 已改写 {len(affected_files)} 个文件（{msg}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
