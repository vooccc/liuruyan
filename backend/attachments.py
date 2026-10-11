"""多模态附件持久化（图片 sha256 内容寻址 + reinflate）。

Stage 1b 实现（docs/architecture_spec.md §3.1.4）：
- 写盘：tool_result.content 里的 image base64 block → sha256 → 写到
  `data/agents/<agent_id>/attachments/<sha256>.<ext>` → 替换为轻量 image_ref
- 读回：to_anthropic_messages adapter 翻译 messages 时遇到 image_ref →
  从盘读 → 重 inflate 回 Anthropic image base64 block

设计要点：
- 内容寻址（sha256）：同一张图被读 N 次只占一份磁盘空间，自然去重
- per-agent 目录：D6 决策（不同 agent 各存各，不全局共享）
- transcript 里永远是轻量引用，体积可控；conversation_history 内存里
  也是 image_ref（除非正在喂给 LLM 的瞬间才 inflate）
"""
from __future__ import annotations

import base64
import hashlib
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .paths import agent_attachments_dir

logger = logging.getLogger(__name__)


# media_type → 文件后缀（写盘用）
_MEDIA_TYPE_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}


def _ext_for(media_type: str) -> str:
    return _MEDIA_TYPE_EXT.get(media_type.lower(), ".bin")


def _persist_image_block(
    block: Dict[str, Any],
    agent_id: str,
) -> Optional[Dict[str, Any]]:
    """单个 Anthropic image block → 落盘 → 返回 image_ref block。

    block 形态：{"type": "image", "source": {"type": "base64", "media_type": "...", "data": "..."}}
    返回：{"type": "image_ref", "sha256": "...", "media_type": "...", "ext": "...", "size_bytes": N}
    解码失败 / 缺字段返 None（调用方应保留原 block 兜底）。
    """
    if not isinstance(block, dict) or block.get("type") != "image":
        return None
    src = block.get("source") or {}
    if not isinstance(src, dict):
        return None
    if src.get("type") != "base64":
        return None
    data = src.get("data")
    media_type = src.get("media_type") or "application/octet-stream"
    if not isinstance(data, str) or not data:
        return None
    try:
        raw = base64.b64decode(data, validate=True)
    except (ValueError, TypeError) as e:
        logger.warning("[attachments] base64 decode failed agent=%s: %s", agent_id, e)
        return None

    sha256 = hashlib.sha256(raw).hexdigest()
    ext = _ext_for(media_type)
    target = agent_attachments_dir(agent_id) / f"{sha256}{ext}"
    if not target.exists():
        try:
            target.write_bytes(raw)
        except OSError as e:
            logger.warning("[attachments] write failed path=%s: %s", target, e)
            return None
    return {
        "type": "image_ref",
        "sha256": sha256,
        "media_type": media_type,
        "ext": ext,
        "size_bytes": len(raw),
    }


def persist_image_blocks_in_content(
    content: Any,
    agent_id: Optional[str],
) -> Any:
    """递归扫 tool_result 等 content 字段，把 image base64 block 替换为 image_ref。

    传入：可能是 str / list[block] / dict
    返回：同结构但所有 image block 已替换为 image_ref（如果落盘成功）
    无 agent_id 时跳过持久化（保留原 base64 block，per-turn 仍可用）。
    """
    if not agent_id:
        return content
    if isinstance(content, list):
        new_list: List[Any] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "image":
                ref = _persist_image_block(item, agent_id)
                new_list.append(ref if ref is not None else item)
            else:
                new_list.append(item)
        return new_list
    return content


def _reinflate_image_ref(
    ref: Dict[str, Any],
    agent_id: Optional[str],
) -> Optional[Dict[str, Any]]:
    """image_ref → 读盘 → Anthropic image base64 block。

    缺字段 / 找不到文件 / 读失败返 None（调用方应降级为文字 placeholder）。
    """
    if not isinstance(ref, dict) or ref.get("type") != "image_ref":
        return None
    sha256 = ref.get("sha256")
    media_type = ref.get("media_type") or "application/octet-stream"
    ext = ref.get("ext") or _ext_for(media_type)
    if not isinstance(sha256, str) or not sha256:
        return None
    if not agent_id:
        return None
    path = agent_attachments_dir(agent_id) / f"{sha256}{ext}"
    if not path.exists():
        logger.warning("[attachments] reinflate miss agent=%s sha256=%s", agent_id, sha256)
        return None
    try:
        raw = path.read_bytes()
    except OSError as e:
        logger.warning("[attachments] reinflate read failed path=%s: %s", path, e)
        return None
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.b64encode(raw).decode("ascii"),
        },
    }


def reinflate_image_refs_in_content(
    content: Any,
    agent_id: Optional[str],
) -> Tuple[Any, int]:
    """tool_result / user 消息 content 里所有 image_ref → 读盘 → image base64 block。

    返回 (new_content, missed_count)。missed_count > 0 表示有 ref 找不到文件，
    上层可以决定是否打点 / 替换为文字 placeholder。
    """
    if not isinstance(content, list):
        return content, 0
    new_list: List[Any] = []
    missed = 0
    for item in content:
        if isinstance(item, dict) and item.get("type") == "image_ref":
            inflated = _reinflate_image_ref(item, agent_id)
            if inflated is not None:
                new_list.append(inflated)
            else:
                missed += 1
                # 降级：替换为文字提示，避免下游 Anthropic SDK 收到陌生 type 报错
                sha = (item.get("sha256") or "?")[:8]
                new_list.append({
                    "type": "text",
                    "text": f"[image attachment unavailable: sha256={sha}...]",
                })
        else:
            new_list.append(item)
    return new_list, missed


__all__ = [
    "persist_image_blocks_in_content",
    "reinflate_image_refs_in_content",
]
