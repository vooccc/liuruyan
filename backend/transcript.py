"""
JSONL 追加写入器 — 会话消息持久化

设计原则：
- 追加写入，每条 flush，崩溃可恢复
- 异步写入，不阻塞主流程
- 行级校验，跳过损坏行
"""
import json
import logging
import os
import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)


def extract_text_content(content: str) -> str:
    """提取消息中的可读文本，兼容 segments JSON。"""
    if not isinstance(content, str):
        return ""

    stripped = content.strip()
    if not stripped.startswith("{"):
        return content

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return content

    if isinstance(parsed, dict):
        segments = parsed.get("segments")
        if isinstance(segments, list):
            text_parts = [
                seg.get("content", "")
                for seg in segments
                if isinstance(seg, dict) and seg.get("type") in {"text", "thinking"}
            ]
            text = "\n".join(part for part in text_parts if part)
            if text:
                return text
        if isinstance(parsed.get("content"), str):
            return parsed["content"]

    return content


class TranscriptWriter:
    """JSONL 追加写入器"""

    def __init__(self, file_path: Path):
        self._path = file_path
        self._lock = asyncio.Lock()
        self._file = None

    async def _ensure_open(self):
        """确保文件已打开（追加模式）"""
        if self._file is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(self._path, 'a', encoding='utf-8')

    async def append(self, entry: Dict) -> None:
        """追加一条记录（线程安全）"""
        async with self._lock:
            await self._ensure_open()
            line = json.dumps(entry, ensure_ascii=False) + '\n'
            self._file.write(line)
            self._file.flush()

    async def append_message(
        self,
        role: str,
        content: str,
        timestamp: str = None,
        client_id: Optional[str] = None,
        message_seq: Optional[int] = None,
        history_revision: Optional[int] = None,
    ) -> None:
        """追加一条消息记录。client_id 用于前端 in-flight cache 跟持久化条目的端到端一对一匹配。

        本方法主要服务 user message 写盘（content 永远是字符串）。
        Assistant message 走 append_assistant_blocks（v3 Anthropic content blocks）。
        """
        entry: Dict = {
            "type": "message",
            "role": role,
            "content": content,
            "timestamp": timestamp or datetime.now().isoformat(),
        }
        if client_id:
            entry["client_id"] = client_id
        if isinstance(message_seq, int) and message_seq > 0:
            entry["message_seq"] = message_seq
        if isinstance(history_revision, int) and history_revision > 0:
            entry["history_revision"] = history_revision
        await self.append(entry)

    async def append_assistant_blocks(
        self,
        content_blocks: List[Dict[str, Any]],
        timestamp: str = None,
        client_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        message_seq: Optional[int] = None,
        history_revision: Optional[int] = None,
    ) -> None:
        """追加一条 Anthropic content blocks 格式的 assistant 消息（v3，唯一写盘 API）。

        内部统一采用 Anthropic Messages API 格式，
        content 是有序 blocks 数组，天然保留 thinking/text/tool_use 的交错顺序。
        LLM 调用时通过 to_openai_messages / to_anthropic_messages adapter 拍扁
        或直接喂给目标 provider。

        Args:
            content_blocks: list of {type, ...}，按 LLM 生成顺序排列。允许的 type：
                - {"type": "thinking", "thinking": "..."}
                - {"type": "text", "text": "..."}
                - {"type": "tool_use", "id": "...", "name": "...", "input": {...}}
            timestamp: ISO 时间戳，默认 now
            client_id: 前端 dedup 追踪 ID
            agent_id: 协作场景下标识来源 agent
        """
        entry: Dict = {
            "type": "message",
            "role": "assistant",
            "content": list(content_blocks or []),
            "timestamp": timestamp or datetime.now().isoformat(),
        }
        if client_id:
            entry["client_id"] = client_id
        if agent_id:
            entry["agent_id"] = agent_id
        if isinstance(message_seq, int) and message_seq > 0:
            entry["message_seq"] = message_seq
        if isinstance(history_revision, int) and history_revision > 0:
            entry["history_revision"] = history_revision
        await self.append(entry)

    async def append_tool_results_user_message(
        self,
        tool_results: List[Dict[str, Any]],
        timestamp: str = None,
        message_seq: Optional[int] = None,
        history_revision: Optional[int] = None,
    ) -> None:
        """[v3] 把 N 个 tool_result 合成一条 user 消息（Anthropic 协议要求）。

        Args:
            tool_results: list of {tool_use_id, content}
                         例：[{"tool_use_id": "toolu_01", "content": "..."}, ...]
                         调用方提前从 segments 拆出来（_segments_to_anthropic_blocks 帮做）
            timestamp: ISO 时间戳，默认 now
        """
        if not tool_results:
            return
        # 标准化 block 形态，确保 type 字段存在
        normalized_blocks: List[Dict[str, Any]] = []
        for tr in tool_results:
            if not isinstance(tr, dict):
                continue
            normalized_blocks.append({
                "type": "tool_result",
                "tool_use_id": tr.get("tool_use_id") or "",
                "content": tr.get("content") or "",
            })
        if not normalized_blocks:
            return
        entry: Dict = {
            "type": "message",
            "role": "user",
            "content": normalized_blocks,
            "timestamp": timestamp or datetime.now().isoformat(),
        }
        if isinstance(message_seq, int) and message_seq > 0:
            entry["message_seq"] = message_seq
        if isinstance(history_revision, int) and history_revision > 0:
            entry["history_revision"] = history_revision
        await self.append(entry)

    async def append_user_image_message(
        self,
        content_blocks: List[Dict[str, Any]],
        timestamp: str = None,
        message_seq: Optional[int] = None,
        history_revision: Optional[int] = None,
    ) -> None:
        """[v3 / 多模态延迟图像投递机制] 把本轮工具产出的 image 拼成一条独立 user 消息写盘。

        发生时机：紧跟 append_tool_results_user_message 之后。模型路径上这条
        message 让模型在 user message.content 合法位置看到真图（不被反代
        tool_result 路径吃图）。事故根因 + 切派理由见
        backend/llm_client.py:_split_text_and_image_blocks docstring。

        Args:
            content_blocks: list[block]，典型形态 ``[text leading, image|image_ref, ...]``
                调用方应提前用 ``backend.attachments.persist_image_blocks_in_content``
                把 image base64 替换成轻量 image_ref（落盘 attachments/<sha>.png）。
            timestamp: ISO 时间戳，默认 now
        """
        if not content_blocks:
            return
        # 防御：过滤掉非 dict / 缺 type 的项，确保盘上结构干净
        cleaned: List[Dict[str, Any]] = [
            b for b in content_blocks
            if isinstance(b, dict) and isinstance(b.get("type"), str)
        ]
        if not cleaned:
            return
        entry: Dict = {
            "type": "message",
            "role": "user",
            "content": cleaned,
            "timestamp": timestamp or datetime.now().isoformat(),
        }
        if isinstance(message_seq, int) and message_seq > 0:
            entry["message_seq"] = message_seq
        if isinstance(history_revision, int) and history_revision > 0:
            entry["history_revision"] = history_revision
        await self.append(entry)

    async def append_emotion(self, emotion: Dict, timestamp: str = None) -> None:
        """追加一条情感状态变更"""
        await self.append({
            "type": "emotion",
            "current": emotion.get("current", "平静"),
            "intensity": emotion.get("intensity", 0.5),
            "timestamp": timestamp or datetime.now().isoformat()
        })

    async def append_mask(self, mask_state: Dict, timestamp: str = None) -> None:
        """追加一条面具状态变更"""
        await self.append({
            "type": "mask",
            "mask": mask_state.get("active_mask"),
            "stage": mask_state.get("stage"),
            "timestamp": timestamp or datetime.now().isoformat()
        })

    async def append_worker(self, worker_id: str, status: str, result: str = None, timestamp: str = None) -> None:
        """追加一条 Worker 状态变更"""
        entry = {
            "type": "worker",
            "worker_id": worker_id,
            "status": status,
            "timestamp": timestamp or datetime.now().isoformat()
        }
        if result is not None:
            entry["result"] = result
        await self.append(entry)

    async def close(self) -> None:
        """关闭文件句柄"""
        if self._file:
            self._file.close()
            self._file = None

    async def edit_message(
        self,
        visible_index: int,
        payload: Dict[str, Any],
        history_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        """编辑指定"用户视角"消息的文本内容（v4 2026-05-22 引入，L2 聊天编辑）。

        "用户视角"索引口径与 ``truncate_after`` 完全一致：
        - role=user 且 content 是 **字符串** → 计数 +1（普通用户输入，可编辑）
        - role=assistant → 计数 +1（一轮 assistant 回复，可编辑 text/thinking 块）
        - role=user 且 content 是 **list**（tool_result / image attachment blocks）
          → **不计数**（artifact，不可编辑——要改请删整轮重做）
        - role=tool（OpenAI 老格式残留）→ **不计数**，不可编辑
        - type != 'message' → 不计数，不可编辑

        Args:
            visible_index: 0-indexed 用户视角索引。例如第 3 条 user/assistant 消息
                是 visible_index=2。
            payload: 编辑负载。两种合法形态：

                1. user 消息（原 content 是字符串）::

                   {"content": "新文本"}

                2. assistant 消息（原 content 是 list[ContentBlock]）::

                   {"block_patches": [
                       {"block_index": 0, "type": "text", "content": "新可见文字"},
                       {"block_index": 2, "type": "thinking", "content": "新思考"},
                   ]}

                   block_index 是该消息 ``content`` 数组中的下标。仅 type=text /
                   type=thinking 块可被打补丁；tool_use 块**严禁修改**（要改请删整
                   轮重做），patch 试图指向 tool_use 块会 ValueError。type 必须与
                   原块匹配，否则 ValueError（防错位）。

        Returns:
            ``{"edited": True, "role": role, "fields_changed": N}`` 或
            ``{"edited": False, "reason": "..."}`` 当 visible_index 越界或目标
            条目不可编辑时。

        Raises:
            ValueError: payload 形态不合法（缺字段、patch 指向 tool_use 块、
                type 不匹配等）。

        线程安全：内部持 ``self._lock``，与 append / truncate_after 互斥。
        """
        if not isinstance(payload, dict):
            raise ValueError("payload 必须是 dict")
        if visible_index < 0:
            return {"edited": False, "reason": "visible_index 必须 ≥ 0"}

        async with self._lock:
            # 1) 关掉 append 句柄（Windows 不能 replace 被占用文件，与 truncate_after 同理）
            if self._file is not None:
                self._file.close()
                self._file = None

            if not self._path.exists():
                return {"edited": False, "reason": "transcript 文件不存在"}

            with open(self._path, 'r', encoding='utf-8') as f:
                raw_lines = f.readlines()

            # 2) 扫描定位 visible_index 对应的物理行；artifact 行不计数但保留
            target_line_idx: Optional[int] = None
            visible = 0
            for idx, raw in enumerate(raw_lines):
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "message":
                    continue
                role = entry.get("role")
                content = entry.get("content")
                is_artifact = (
                    (role == "user" and isinstance(content, list))
                    or role == "tool"
                )
                if is_artifact:
                    continue
                if visible == visible_index:
                    target_line_idx = idx
                    break
                visible += 1

            if target_line_idx is None:
                return {"edited": False, "reason": f"visible_index={visible_index} 越界（共 {visible} 条可见消息）"}

            # 3) 解析目标条目并按 role 应用编辑
            target_entry = json.loads(raw_lines[target_line_idx].strip())
            role = target_entry.get("role")
            fields_changed = 0

            if role == "user":
                # user 消息：content 必须原本就是字符串才允许编辑
                if not isinstance(target_entry.get("content"), str):
                    return {"edited": False, "reason": "user 消息 content 不是字符串（疑似 artifact，不应被定位到）"}
                new_content = payload.get("content")
                if not isinstance(new_content, str):
                    raise ValueError("user 消息编辑需 payload.content 为字符串")
                if new_content != target_entry["content"]:
                    target_entry["content"] = new_content
                    fields_changed = 1

            elif role == "assistant":
                # assistant 消息：content 必须是 list[block]；按 block_patches 逐项打补丁
                blocks = target_entry.get("content")
                if not isinstance(blocks, list):
                    return {"edited": False, "reason": "assistant content 不是 list（非 v3 格式）"}
                patches = payload.get("block_patches")
                if not isinstance(patches, list):
                    raise ValueError("assistant 消息编辑需 payload.block_patches 为 list")
                for patch in patches:
                    if not isinstance(patch, dict):
                        raise ValueError("block_patches 元素必须是 dict")
                    bidx = patch.get("block_index")
                    btype = patch.get("type")
                    bcontent = patch.get("content")
                    if not isinstance(bidx, int) or bidx < 0 or bidx >= len(blocks):
                        raise ValueError(f"block_index={bidx} 越界（共 {len(blocks)} 块）")
                    if btype not in ("text", "thinking"):
                        raise ValueError(f"只支持编辑 text/thinking 块，不支持 type={btype!r}")
                    if not isinstance(bcontent, str):
                        raise ValueError("block_patches[].content 必须是字符串")
                    target_block = blocks[bidx]
                    if not isinstance(target_block, dict):
                        raise ValueError(f"目标块 #{bidx} 不是 dict")
                    orig_type = target_block.get("type")
                    if orig_type != btype:
                        raise ValueError(f"block #{bidx} 原类型为 {orig_type!r}，与 patch.type={btype!r} 不匹配（防错位）")
                    # Anthropic content block 字段名跟 type 同名：text→"text"，thinking→"thinking"
                    field_key = btype  # "text" or "thinking"
                    if target_block.get(field_key) != bcontent:
                        target_block[field_key] = bcontent
                        fields_changed += 1
                # 写回 blocks（已就地修改，赋值是冗余的，但显式更稳）
                target_entry["content"] = blocks

            else:
                return {"edited": False, "reason": f"role={role!r} 不支持编辑"}

            if fields_changed == 0:
                return {"edited": False, "reason": "内容无变化"}

            # 4) 写回目标行；保留其它行不变
            if isinstance(history_revision, int) and history_revision > 0:
                target_entry["history_revision"] = history_revision
            raw_lines[target_line_idx] = json.dumps(target_entry, ensure_ascii=False) + '\n'

            # 5) 原子替换（与 truncate_after 同手法）
            tmp_path = self._path.with_suffix(self._path.suffix + '.tmp')
            with open(tmp_path, 'w', encoding='utf-8', newline='') as f:
                f.writelines(raw_lines)
            os.replace(tmp_path, self._path)

            return {
                "edited": True,
                "role": role,
                "fields_changed": fields_changed,
                "visible_index": visible_index,
            }

    async def delete_visible_message(self, visible_index: int) -> Dict[str, Any]:
        """原子删除单条"用户视角"消息（v5 2026-05-26 引入，对齐 Cherry "选哪条删哪条"）。

        "用户视角"索引口径与 ``truncate_after`` / ``edit_message`` 完全一致：
        - role=user 且 content 是字符串 → 计数 +1（普通用户输入）
        - role=assistant → 计数 +1（一轮 assistant 回复）
        - role=user 且 content 是 list（tool_result blocks / image attachment）
          → 不计数（artifact，**跟随其前面那条 assistant 一起被删**）
        - role=tool（OpenAI 老格式残留）→ 不计数，artifact，同上
        - type != 'message'（emotion/mask/worker/...）→ **不计数也不删**（会话级状态）

        删除范围：[X, Y) 区间内**所有 message 行**，其中：
          - X = 第 ``visible_index`` 条 visible message 的物理行
          - Y = 下一条 visible message 的物理行（不存在则 EOF，但只删 message 行）
          - 中间的 non-message 行（emotion/mask/worker）原样保留

        这等价于"删整条 assistant turn（含其 tool_result artifacts）"或"删单条
        user 输入"，永远不会留下孤儿 tool_use 没有 tool_result 配对，下次 LLM
        调用零 400 风险。

        Args:
            visible_index: 0-indexed 用户视角索引。

        Returns:
            ``{"deleted": True, "role": role, "removed_lines": N, "visible_index": i}``
            或 ``{"deleted": False, "reason": "..."}`` 当越界。

        线程安全：内部持 ``self._lock``，与 append / truncate_after / edit_message 互斥。
        """
        if visible_index < 0:
            return {"deleted": False, "reason": "visible_index 必须 ≥ 0"}

        async with self._lock:
            # 1) 关掉 append 句柄（Windows 不能 replace 被占用文件）
            if self._file is not None:
                self._file.close()
                self._file = None

            if not self._path.exists():
                return {"deleted": False, "reason": "transcript 文件不存在"}

            with open(self._path, 'r', encoding='utf-8') as f:
                raw_lines = f.readlines()

            def _classify(raw: str) -> Tuple[str, Optional[str]]:
                """分类一行：('non_message', None) / ('artifact', role) / ('visible', role) / ('skip', None)"""
                stripped = raw.strip()
                if not stripped:
                    return ('skip', None)
                try:
                    entry = json.loads(stripped)
                except json.JSONDecodeError:
                    return ('skip', None)
                if entry.get("type") != "message":
                    return ('non_message', None)
                role = entry.get("role")
                content = entry.get("content")
                is_artifact = (
                    (role == "user" and isinstance(content, list))
                    or role == "tool"
                )
                if is_artifact:
                    return ('artifact', role)
                return ('visible', role)

            # 2) 扫一遍：定位 X = 第 visible_index 条 visible 消息行；
            #    确定其 role；累计 visible 总数用于错误信息
            target_line_idx: Optional[int] = None
            target_role: Optional[str] = None
            visible = 0
            for idx, raw in enumerate(raw_lines):
                kind, role = _classify(raw)
                if kind == 'visible':
                    if visible == visible_index:
                        target_line_idx = idx
                        target_role = role
                        # 找到后继续往下扫一遍统计 visible 总数（错误信息友好）
                    visible += 1

            if target_line_idx is None:
                return {
                    "deleted": False,
                    "reason": f"visible_index={visible_index} 越界（共 {visible} 条可见消息）",
                }

            # 3) 第二遍：从 X 开始扫到下一条 visible（exclusive），收集要删的行号；
            #    artifact 行删，message 类无关行（empty/corrupted）也删（前向归属本轮），
            #    non_message 行（emotion/mask/worker）保留。
            lines_to_delete: List[int] = [target_line_idx]
            for idx in range(target_line_idx + 1, len(raw_lines)):
                kind, _ = _classify(raw_lines[idx])
                if kind == 'visible':
                    break  # 撞到下一条 visible 消息，停
                if kind == 'non_message':
                    continue  # 保留 emotion/mask/worker
                # artifact / skip（empty 或 corrupted）→ 删（属于本轮副产物）
                lines_to_delete.append(idx)

            if not lines_to_delete:
                return {"deleted": False, "reason": "未找到可删除的行"}

            delete_set = set(lines_to_delete)
            kept_lines = [raw for idx, raw in enumerate(raw_lines) if idx not in delete_set]

            # 4) 原子重写
            tmp_path = self._path.with_suffix(self._path.suffix + '.tmp')
            with open(tmp_path, 'w', encoding='utf-8', newline='') as f:
                f.writelines(kept_lines)
            os.replace(tmp_path, self._path)

            return {
                "deleted": True,
                "role": target_role,
                "removed_lines": len(lines_to_delete),
                "visible_index": visible_index,
            }

    async def truncate_after(self, keep_count: int) -> Dict[str, int]:
        """原子截断：保留前 ``keep_count`` 条"用户视角" message，其后所有行删除。

        "用户视角" 的口径（与前端 messages 数组一一对应）：
        - role=user 且 content 是 **字符串** → 计数 +1（普通用户输入）
        - role=assistant → 计数 +1（一轮 assistant 回复）
        - role=user 且 content 是 **list**（tool_result blocks / image attachment blocks）
          → 不计数（assistant 工具调用的副产物），但**跟随它前面那条 assistant
          一起保留**——否则下次喂 LLM 时 tool_use 没有 tool_result 配对会 400。
        - role=tool（OpenAI std）→ 不计数（同上）
        - type != 'message' 的行（emotion/mask/worker/...）→ 原样保留，不参与计数

        实现：
        - 读全文（按物理行）→ 扫一遍累计 keep_until_line（exclusive）
        - 一旦 visible 数达到 keep_count，停止累积（但允许尾随 artifact 行加入，
          见上面"跟随保留"规则——实际上跟随是通过"先把 artifact 算进 keep_until_line
          再 break"实现）
        - 用 tmp 文件 + os.replace 原子替换原文件
        - 截断前先 close 自己持有的 append 句柄；调用方负责后续重新初始化

        Args:
            keep_count: 保留的用户视角消息数。<=0 表示清空所有 message 行
                （非 message 行仍保留）。

        Returns:
            ``{"removed_lines": N, "kept_visible": K}`` —— 删了 N 行，保留 K 条用户
            视角消息。

        线程安全：内部持 ``self._lock``。
        """
        if keep_count < 0:
            keep_count = 0
        async with self._lock:
            # 1) 关掉 append 句柄，让 os.replace 在 Windows 上安全
            #    （Windows 不能 replace 一个被另一个句柄占用的文件）
            if self._file is not None:
                self._file.close()
                self._file = None

            if not self._path.exists():
                return {"removed_lines": 0, "kept_visible": 0}

            with open(self._path, 'r', encoding='utf-8') as f:
                raw_lines = f.readlines()

            visible = 0
            keep_until_line = 0  # exclusive
            stopped = False
            for idx, raw in enumerate(raw_lines):
                stripped = raw.strip()
                if not stripped:
                    if not stopped:
                        keep_until_line = idx + 1
                    continue
                try:
                    entry = json.loads(stripped)
                except json.JSONDecodeError:
                    if not stopped:
                        keep_until_line = idx + 1
                    continue

                if entry.get("type") != "message":
                    if not stopped:
                        keep_until_line = idx + 1
                    continue

                role = entry.get("role")
                content = entry.get("content")
                is_artifact = (
                    (role == "user" and isinstance(content, list))
                    or role == "tool"
                )

                if is_artifact:
                    if stopped:
                        continue
                    keep_until_line = idx + 1
                    continue

                # 用户视角消息
                if visible >= keep_count:
                    stopped = True
                    continue
                visible += 1
                keep_until_line = idx + 1

            removed = len(raw_lines) - keep_until_line
            if removed <= 0:
                return {"removed_lines": 0, "kept_visible": visible}

            # 2) 原子重写：先写 tmp 再 os.replace
            #    用 'w' newline='' 避免 Windows 把 \n 翻成 \r\n（原 jsonl 写入时
            #    走的是默认 'a' 模式 ↔ 此处保持纯 \n 一致）
            tmp_path = self._path.with_suffix(self._path.suffix + '.tmp')
            with open(tmp_path, 'w', encoding='utf-8', newline='') as f:
                f.writelines(raw_lines[:keep_until_line])
            os.replace(tmp_path, self._path)

            return {"removed_lines": removed, "kept_visible": visible}


class TranscriptReader:
    """JSONL 读取器 — 支持全量读取、最近 N 条、消息计数"""

    def __init__(self, file_path: Path):
        self._path = file_path

    def read_all(self) -> List[Dict]:
        """读取所有记录，跳过损坏行"""
        if not self._path.exists():
            return []
        records = []
        with open(self._path, 'r', encoding='utf-8') as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    # 跳过损坏行，记录日志
                    logger.warning("[TRANSCRIPT] Skipped corrupted line %s in %s", line_no, self._path.name)
        return records

    def read_messages(self) -> List[Dict]:
        """只读取 type=message 的记录"""
        return [r for r in self.read_all() if r.get("type") == "message"]

    def backfill_message_metadata(self) -> Dict[str, int]:
        if not self._path.exists():
            return {"changed": 0, "max_message_seq": 0, "max_history_revision": 0}

        raw_lines = self._path.read_text(encoding="utf-8").splitlines(keepends=True)
        running_message_seq = 0
        running_history_revision = 0
        previous_message_was_assistant = False
        changed = 0
        rewritten_lines: List[str] = []

        for raw_line in raw_lines:
            try:
                entry = json.loads(raw_line.strip())
            except json.JSONDecodeError:
                rewritten_lines.append(raw_line)
                continue

            if not isinstance(entry, dict) or entry.get("type") != "message":
                rewritten_lines.append(raw_line)
                continue

            raw_message_seq = entry.get("message_seq")
            if (
                isinstance(raw_message_seq, int)
                and not isinstance(raw_message_seq, bool)
                and raw_message_seq > running_message_seq
            ):
                running_message_seq = raw_message_seq
            else:
                running_message_seq += 1
                entry["message_seq"] = running_message_seq
                changed += 1

            role = entry.get("role")
            content = entry.get("content")
            is_artifact = role == "tool" or (role == "user" and isinstance(content, list))
            raw_history_revision = entry.get("history_revision")
            if (
                isinstance(raw_history_revision, int)
                and not isinstance(raw_history_revision, bool)
                and raw_history_revision >= running_history_revision
                and raw_history_revision > 0
            ):
                running_history_revision = raw_history_revision
            else:
                if not is_artifact or not previous_message_was_assistant:
                    running_history_revision += 1
                entry["history_revision"] = max(1, running_history_revision)
                running_history_revision = entry["history_revision"]
                changed += 1

            previous_message_was_assistant = role == "assistant" or (
                is_artifact and previous_message_was_assistant
            )
            rewritten_lines.append(json.dumps(entry, ensure_ascii=False) + "\n")

        if changed:
            tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
            with open(tmp_path, "w", encoding="utf-8", newline="") as file_obj:
                file_obj.writelines(rewritten_lines)
            os.replace(tmp_path, self._path)

        return {
            "changed": changed,
            "max_message_seq": running_message_seq,
            "max_history_revision": running_history_revision,
        }

    def read_recent_messages(self, n: int = 20) -> List[Dict]:
        """读取最近 N 条消息"""
        messages = self.read_messages()
        return messages[-n:] if len(messages) > n else messages

    def count_messages(self) -> int:
        """统计消息数量"""
        return len(self.read_messages())

    def rebuild_conversation_history(self) -> List[Dict]:
        """从 transcript 重建对话历史（v3 单格式）。

        当前唯一合法格式（v3）：
        - role=assistant: content=list[ContentBlock]（thinking/text/tool_use 交错）
          或 content=str（纯文本轮次，无 tool）
        - role=user: content=str 或 content=list[ContentBlock]（含 tool_result）
        - role=tool: 独立 entry（早期 v2 残留，v3 已不产出但带勿击）

        Phase E (2026-05-20) 后不再兑底老格式 —— 检测到 v1 segments JSON
        string 格式 → fail-fast raise RuntimeError。出现则说明从 Phase E 之前
        的备份恢复了老数据；需要补一个迁移脚本才能重入。

        若记录带 client_id 字段，原样保留供前端 dedup 一对一匹配。
        """
        messages = self.read_messages()
        rebuilt: List[Dict] = []
        running_message_seq = 0
        for idx, m in enumerate(messages):
            role = m.get("role")
            if role not in ("user", "assistant", "tool"):
                continue
            # Phase E fail-fast：检测 v1 legacy segments JSON string
            if role == "assistant":
                content = m.get("content")
                if (
                    isinstance(content, str)
                    and content.lstrip().startswith("{")
                    and '"segments"' in content
                ):
                    raise RuntimeError(
                        f"[transcript] 检测到 Phase E 之前的老格式 assistant entry"
                        f"（{self._path.name} entry #{idx}）。如从老备份恢复，"
                        f"需补迁移脚本转成 v3 Anthropic content blocks 格式。"
                        f"参考：docs/architecture_spec.md"
                    )
            raw_message_seq = m.get("message_seq")
            if isinstance(raw_message_seq, int) and not isinstance(raw_message_seq, bool) and raw_message_seq > running_message_seq:
                running_message_seq = raw_message_seq
            else:
                running_message_seq += 1
            entry: Dict = {
                "role": role,
                "content": m.get("content", ""),
                "timestamp": m.get("timestamp"),
                "message_seq": running_message_seq,
            }
            raw_history_revision = m.get("history_revision")
            if isinstance(raw_history_revision, int) and not isinstance(raw_history_revision, bool) and raw_history_revision > 0:
                entry["history_revision"] = raw_history_revision
            # 透传结构化字段
            if "tool_calls" in m:
                entry["tool_calls"] = m["tool_calls"]
            if "tool_call_id" in m:
                entry["tool_call_id"] = m["tool_call_id"]
            if "reasoning_content" in m:
                entry["reasoning_content"] = m["reasoning_content"]
            if "name" in m:
                entry["name"] = m["name"]
            client_id = m.get("client_id")
            if client_id:
                entry["client_id"] = client_id
            # 协作场景下标识来源 agent；不透传会导致重启后 get_session 把 segments
            # 里的 agent_id 全填 None，前端 collab 历史 bubble 失去归属，头像/名字
            # 退回到 "__assistant__" 兜底（变成首字 "角"）。
            agent_id = m.get("agent_id")
            if agent_id:
                entry["agent_id"] = agent_id
            rebuilt.append(entry)
        return rebuilt

    def get_last_emotion(self) -> Optional[Dict]:
        """获取最后一条情感状态"""
        records = self.read_all()
        for r in reversed(records):
            if r.get("type") == "emotion":
                return {"current": r.get("current"), "intensity": r.get("intensity")}
        return None

    def get_last_mask_state(self) -> Optional[Dict]:
        """获取最后一条面具状态"""
        records = self.read_all()
        for r in reversed(records):
            if r.get("type") == "mask":
                return {"active_mask": r.get("mask"), "stage": r.get("stage")}
        return None

    def recall_snippets(
        self,
        keywords: List[str],
        preferred_span: Optional[List[int]] = None,
        max_snippets: int = 3,
        window: int = 1,
    ) -> List[Dict]:
        """按关键词和来源范围召回 transcript 片段。"""
        raw_messages = self.read_messages()
        if not raw_messages:
            return []

        messages = [
            {
                "index": i + 1,
                "role": msg.get("role", "user"),
                "content": extract_text_content(msg.get("content", "")).strip(),
            }
            for i, msg in enumerate(raw_messages)
        ]

        cleaned_keywords = []
        for keyword in keywords:
            kw = (keyword or "").strip().lower()
            if kw and kw not in cleaned_keywords:
                cleaned_keywords.append(kw)

        def score_message(message: Dict) -> int:
            score = 0
            text = message["content"].lower()
            if preferred_span and len(preferred_span) == 2 and preferred_span[0] <= message["index"] <= preferred_span[1]:
                score += 6
            for kw in cleaned_keywords:
                if kw in text:
                    score += 3 if len(kw) >= 4 else 2
            return score

        scored = []
        for idx, message in enumerate(messages):
            score = score_message(message)
            if score > 0:
                scored.append((score, idx))

        if not scored and preferred_span and len(preferred_span) == 2:
            start = max(0, preferred_span[0] - 1)
            end = min(len(messages), preferred_span[1])
            if start < end:
                scored.append((1, (start + end - 1) // 2))

        if not scored:
            return []

        snippets = []
        used_ranges = []
        for score, center_idx in sorted(scored, key=lambda item: item[0], reverse=True):
            start_idx = max(0, center_idx - window)
            end_idx = min(len(messages), center_idx + window + 1)
            if preferred_span and len(preferred_span) == 2 and preferred_span[0] <= messages[center_idx]["index"] <= preferred_span[1]:
                start_idx = min(start_idx, max(0, preferred_span[0] - 1))
                end_idx = max(end_idx, min(len(messages), preferred_span[1]))

            current_range = (start_idx, end_idx)
            if any(not (end_idx <= s or start_idx >= e) for s, e in used_ranges):
                continue

            chunk = messages[start_idx:end_idx]
            snippet_lines = [f'[{item["index"]}] {"用户" if item["role"] == "user" else "助手"}：{item["content"]}' for item in chunk if item["content"]]
            if not snippet_lines:
                continue

            snippets.append({
                "score": score,
                "span": [chunk[0]["index"], chunk[-1]["index"]],
                "text": "\n".join(snippet_lines),
            })
            used_ranges.append(current_range)
            if len(snippets) >= max_snippets:
                break

        return snippets
