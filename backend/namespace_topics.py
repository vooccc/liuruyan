"""项目命名空间“树叉层”（topics/*.md）纯存储与按需摘枝模块。

职责：
- 每个项目空间（namespaces/{ns_id}/）下的 `topics/{topic_name}.md` 保存该项目的专题知识；
- `FACTS.md` 作为精炼的树干根节点（常驻注入），`topics/*.md` 按当前话题动态摘取 1~2 个注入；
- 保持零外部依赖（不依赖 LLM / Agent / Session），所有文件写操作配合 NamespaceManager 的 RLock 原子落盘。
"""
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import re
from typing import Any, Callable, Dict, List, Optional


MAX_TOPICS_SOFT_LIMIT = 8
MAX_TOPIC_INJECT_COUNT = 2
TOPIC_INJECT_BUDGET_CHARS = 3200
TOPIC_MERGE_MAX_CHARS = 6000

_ILLEGAL_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")


def sanitize_topic_name(name: str) -> str:
    """规范化树叉名，防止路径穿越和非法文件名字符。"""
    raw = str(name or "").strip()
    if not raw:
        return ""
    cleaned = raw.replace("..", "_")
    cleaned = _ILLEGAL_FILENAME_RE.sub("_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._-")
    if len(cleaned) > 24:
        cleaned = cleaned[:24].rstrip(" ._-")
    return cleaned


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def extract_topic_summary(content: str, max_len: int = 80) -> str:
    """从树叉 Markdown 正文提取干净的一行摘要。"""
    for line in (content or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("<!--") or s.startswith("---"):
            continue
        s = re.sub(r"^[-*+]\s+", "", s)
        s = re.sub(r"^\d+\.\s+", "", s)
        s = _MD_LINK_RE.sub(r"\1", s)
        s = s.replace("**", "").replace("__", "").replace("`", "")
        s = re.sub(r"\s+", " ", s).strip()
        if not s:
            continue
        if len(s) <= max_len:
            return s
        return s[: max_len - 1].rstrip() + "…"
    return ""


def _topics_dir(ns_dir: Path) -> Path:
    return ns_dir / "topics"


def _topic_path(ns_dir: Path, topic_name: str) -> Optional[Path]:
    safe = sanitize_topic_name(topic_name)
    if not safe:
        return None
    return _topics_dir(ns_dir) / f"{safe}.md"


def list_topics(ns_dir: Path) -> List[Dict[str, Any]]:
    """列出项目下全部专题树叉，按最近更新时间倒序。"""
    tdir = _topics_dir(ns_dir)
    if not tdir.exists() or not tdir.is_dir():
        return []
    items: List[Dict[str, Any]] = []
    for p in tdir.glob("*.md"):
        try:
            stat = p.stat()
            text = p.read_text(encoding="utf-8")
            items.append({
                "name": p.stem,
                "filename": p.name,
                "summary": extract_topic_summary(text),
                "chars": len(text),
                "updated_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                "_mtime": stat.st_mtime,
            })
        except OSError:
            continue
    items.sort(key=lambda x: x.get("_mtime", 0), reverse=True)
    for it in items:
        it.pop("_mtime", None)
    return items


def read_topic(ns_dir: Path, topic_name: str) -> str:
    path = _topic_path(ns_dir, topic_name)
    if path is None or not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def write_topic(
    ns_dir: Path,
    topic_name: str,
    content: str,
    writer: Callable[[Path, str], None] = _atomic_write,
) -> str:
    safe = sanitize_topic_name(topic_name)
    if not safe:
        raise ValueError("树叉名称不能为空或全为非法字符")
    path = _topics_dir(ns_dir) / f"{safe}.md"
    normalized = (content or "").rstrip() + "\n"
    writer(path, normalized)
    return safe


def delete_topic(ns_dir: Path, topic_name: str) -> bool:
    path = _topic_path(ns_dir, topic_name)
    if path is None or not path.exists():
        return False
    path.unlink()
    return True


def merge_topics(
    ns_dir: Path,
    source_topic: str,
    target_topic: str,
    merged_content: Optional[str] = None,
) -> str:
    """合并或重命名树叉：将 source_topic 合并入 target_topic 并删除 source。"""
    src_safe = sanitize_topic_name(source_topic)
    tgt_safe = sanitize_topic_name(target_topic)
    if not src_safe or not tgt_safe:
        raise ValueError("源树叉与目标树叉名称均不能为空")

    src_path = _topics_dir(ns_dir) / f"{src_safe}.md"
    if not src_path.exists():
        raise FileNotFoundError(f"源树叉不存在: {src_safe}")

    src_text = src_path.read_text(encoding="utf-8")
    if merged_content is not None:
        final_text = merged_content
    elif src_safe == tgt_safe:
        final_text = src_text
    else:
        tgt_text = read_topic(ns_dir, tgt_safe).strip()
        if not tgt_text:
            # 重命名：尽量替换首行标题
            lines = src_text.splitlines()
            if lines and lines[0].lstrip().startswith("#"):
                lines[0] = f"# {tgt_safe}"
                final_text = "\n".join(lines)
            else:
                final_text = f"# {tgt_safe}\n\n{src_text.strip()}"
        else:
            # 简单去重合并：保留目标全文，把源树叉非标题且未在目标中出现的行追加进来
            tgt_lines_set = {ln.strip() for ln in tgt_text.splitlines() if ln.strip()}
            extra_lines = [
                ln for ln in src_text.splitlines()
                if ln.strip()
                and not ln.lstrip().startswith("# ")
                and ln.strip() not in tgt_lines_set
            ]
            if extra_lines:
                final_text = tgt_text + f"\n\n### 合并自「{src_safe}」\n" + "\n".join(extra_lines)
            else:
                final_text = tgt_text

    write_topic(ns_dir, tgt_safe, final_text)
    if src_safe != tgt_safe and src_path.exists():
        src_path.unlink()
    return tgt_safe


def _tokenize_for_topic_match(query_text: str) -> tuple[List[str], List[str]]:
    """返回 `(完整词/短语 tokens, 中文 2-gram tokens)`（全部小写）。"""
    q = (query_text or "").strip().lower()
    if not q:
        return [], []
    words: List[str] = []
    seen_w = set()
    for tok in re.findall(r"[a-z0-9_.\-]{2,}|[\u4e00-\u9fff]{2,}", q):
        if tok not in seen_w:
            seen_w.add(tok)
            words.append(tok)

    bigrams: List[str] = []
    seen_bg = set(seen_w)
    for seg in re.findall(r"[\u4e00-\u9fff]{3,}", q):
        for i in range(len(seg) - 1):
            bg = seg[i : i + 2]
            if bg not in seen_bg:
                seen_bg.add(bg)
                bigrams.append(bg)
    return words, bigrams


def select_relevant_topics(
    ns_dir: Path,
    query_text: str,
    max_count: int = MAX_TOPIC_INJECT_COUNT,
    max_total_chars: int = TOPIC_INJECT_BUDGET_CHARS,
) -> List[Dict[str, Any]]:
    """按当前话题从 topics/*.md 中精准摘取最相关的 1~2 个树叉。"""
    if not query_text or not query_text.strip() or max_count <= 0:
        return []
    tdir = _topics_dir(ns_dir)
    if not tdir.exists() or not tdir.is_dir():
        return []

    q_low = query_text.strip().lower()
    words, bigrams = _tokenize_for_topic_match(query_text)
    if not words and not bigrams:
        return []

    scored: List[tuple[int, float, Dict[str, Any]]] = []
    for p in tdir.glob("*.md"):
        try:
            stat = p.stat()
            content = p.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not content:
            continue

        name = p.stem
        name_low = name.lower()
        summary = extract_topic_summary(content)
        summary_low = summary.lower()
        body_low = content.lower()

        score = 0
        if len(name_low) >= 2 and name_low in q_low:
            score += 12
        for w in words:
            if w in name_low:
                score += 6
            if w in summary_low:
                score += 4
            if w in body_low:
                score += 2
        for bg in bigrams:
            if bg in name_low:
                score += 2
            if bg in summary_low:
                score += 1
            if bg in body_low:
                score += 1

        if score > 0:
            scored.append((
                score,
                stat.st_mtime,
                {
                    "name": name,
                    "summary": summary,
                    "content": content,
                    "score": score,
                },
            ))

    if not scored:
        return []

    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    picked: List[Dict[str, Any]] = []
    used_chars = 0
    for _, _, item in scored[:max_count]:
        content = item["content"]
        remaining = max_total_chars - used_chars
        if remaining <= 120:
            break
        if len(content) > remaining:
            content = content[: remaining - 12].rstrip() + "\n…（树叉已截断）"
        picked.append({**item, "content": content})
        used_chars += len(content)
    return picked


def render_topics_catalog(topics: List[Dict[str, Any]]) -> str:
    """渲染树叉目录清单，常驻注入让模型知道有哪些专题可按需读取。"""
    if not topics:
        return ""
    lines = []
    for t in topics:
        name = t.get("name") or ""
        summary = (t.get("summary") or "").strip()
        suffix = f"：{summary}" if summary else ""
        lines.append(f"- **{name}**{suffix}")
    return "\n".join(lines)
