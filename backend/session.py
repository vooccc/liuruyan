"""
会话管理核心 — Session / SessionManager / AgentProfile

多会话架构核心实现：
- Session: 会话数据模型，含独立的情感/面具状态和对话历史
- SessionManager: 全局会话生命周期管理
- AgentProfile: 角色共享层管理

Phase 0 目标：兼容现有 API，内部重构为 Session 类 + JSONL
"""
import json
import asyncio
import logging
import uuid
import shutil
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)

from .transcript import TranscriptWriter, TranscriptReader
from .context_checkpoint import (
    invalidate_context_checkpoint as remove_context_checkpoint_file,
    load_context_checkpoint as read_context_checkpoint_file,
    save_context_checkpoint as write_context_checkpoint_file,
    validate_context_checkpoint,
)
from .scope_item import (
    ScopeItem,
    dump_scope_items,
    parse_scope_items,
)


BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parent
from . import paths
DEFAULT_SESSION_BASE_DIR = paths.sessions_dir()
DEFAULT_AGENTS_DIR = paths.agents_dir()


# ====== 默认会话标题匹配 ======
_DEFAULT_TITLE_RE = re.compile(r"^(新对话|与.+的对话)$")


def is_default_session_title(title: Optional[str]) -> bool:
    """判定标题是否为系统新建时的默认占位符（如 '新对话' 或 '与风浪的对话'）。"""
    if not title:
        return True
    return bool(_DEFAULT_TITLE_RE.match(title.strip()))


# ====== 默认情感状态 ======
DEFAULT_EMOTION = {
    "current": "平静",
    "intensity": 0.5,
    "history": [],
    "spectrum": {},
    "arc": {}
}

DEFAULT_MASK_STATE = {
    "active_mask": None,
    "stage": None,
    "awakening_progress": 0.0
}


# ====== Session 数据模型 ======
@dataclass
class Session:
    """会话核心数据结构"""
    # 身份
    id: str
    agent_id: str
    session_type: str = "main"  # "main" | "worker"
    root_session_id: Optional[str] = None
    parent_session_id: Optional[str] = None
    owner_user_id: str = "default"

    # 归属（单角色 or 议事厅房间）
    owner_type: str = "agent"  # "agent" | "room"
    owner_id: str = ""  # agent_id 或 room_id；空值时由 agent_id 派生

    # 协作标签（前端 RoundtableList 等仍读 metadata 镜像）
    collab_type: Optional[str] = None  # "scene" | None
    scene_id: Optional[str] = None
    scene_type: Optional[str] = None  # "free" | "encounter" | "conflict" | … （仅 collab_type='scene' 时有）
    participants: List[str] = field(default_factory=list)
    derived_from: Optional[str] = None  # 预留：扩员来源 session_id

    # 状态
    status: str = "active"  # "active" | "idle" | "suspended" | "terminated"
    created_at: str = ""
    updated_at: str = ""
    last_active_at: str = ""

    # 隔离数据（内存中持有，持久化到各自文件）
    emotion_state: Dict = field(default_factory=lambda: DEFAULT_EMOTION.copy())
    mask_state: Optional[Dict] = None
    conversation_history: List[Dict] = field(default_factory=list)

    # 元数据
    title: str = "新对话"
    message_count: int = 0
    history_revision: int = 0
    next_message_seq: int = 1
    next_event_seq: int = 1
    # per-session model override。语义：“用户在该会话主动切过 picker”。
    # 仅当 chat handler 是 request.model 显式给了才写入。
    model: Optional[str] = None
    # 与 model 配对的 provider id。同一 model id 可能存在于多个 provider下（典型：
    # 多家 anthropic 反代都有 claude-opus-4-7），仅靠 model id 反查会命中第一家。
    # 重启后前端 hydrate 靠这个字段精准定位 picker 选中项，避免“同名模型走错家”
    # 事故（2026-05-12）。依赖于 chat handler 同时写 model+provider。
    provider: Optional[str] = None
    metadata: Dict = field(default_factory=dict)

    # Token 累计消耗（per-session 独立、跨重启保留）
    # 字段口径见 plan：input/output/cache_read/cache_create 是绝对累加值，
    # total = input + output + cache_read + cache_create，calls = LLM 调用次数。
    # OpenAI 兼容协议 cache_* 始终为 0；Anthropic 原生才有。
    token_usage: Dict = field(default_factory=lambda: {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_create": 0,
        "total": 0,
        "calls": 0,
        "last_call_at": None,
    })

    # Pinned context 上下文图钉注入与指导原则机制
    # pinned_guideline: 用户钉的会话级指令，每轮拼 system 时附加。
    # pinned_scope: List[ScopeItem.to_dict()]；持久化存原始 dict 形态、内存中也是 dict
    # （避免 dataclass 嵌套导致序列化复杂度）。读取时再用 parse_scope_items() 解析为对象。
    pinned_guideline: Optional[str] = None
    pinned_scope: List[Dict[str, Any]] = field(default_factory=list)

    # Worker 专属
    task_description: Optional[str] = None
    task_result: Optional[str] = None

    # 运行时（不持久化）
    _transcript_writer: Optional[TranscriptWriter] = field(default=None, repr=False)
    _session_dir: Optional[Path] = field(default=None, repr=False)

    def __post_init__(self):
        now = datetime.now().isoformat()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now
        if not self.last_active_at:
            self.last_active_at = now
        if not self.root_session_id:
            self.root_session_id = self.id
        # owner_id 兜底：agent 归属时派生自 agent_id
        if not self.owner_id and self.owner_type == "agent":
            self.owner_id = self.agent_id
        # metadata 镜像：前端 Sidebar/RoundtableList 仍读 metadata.collab_type 等
        self._sync_metadata_mirror()

    def _sync_metadata_mirror(self) -> None:
        """把顶层协作字段镜像写进 metadata，保持前端契约兼容"""
        if not isinstance(self.metadata, dict):
            self.metadata = {}
        if self.collab_type is not None:
            self.metadata["collab_type"] = self.collab_type
        if self.scene_id is not None:
            self.metadata["scene_id"] = self.scene_id
        if self.scene_type is not None:
            self.metadata["scene_type"] = self.scene_type
        if self.participants:
            self.metadata["participants"] = list(self.participants)
        if self.derived_from is not None:
            self.metadata["derived_from"] = self.derived_from

    @property
    def session_dir(self) -> Path:
        if self._session_dir:
            return self._session_dir
        return DEFAULT_SESSION_BASE_DIR / self.id

    @session_dir.setter
    def session_dir(self, value: Path):
        self._session_dir = value

    @property
    def transcript_path(self) -> Path:
        return self.session_dir / "transcript.jsonl"

    @property
    def session_meta_path(self) -> Path:
        return self.session_dir / "session.json"

    @property
    def emotion_path(self) -> Path:
        return self.session_dir / "emotion.json"

    @property
    def mask_state_path(self) -> Path:
        return self.session_dir / "mask_state.json"

    @property
    def context_checkpoint_path(self) -> Path:
        return self.session_dir / "context_checkpoint.json"

    def load_context_checkpoint(
        self,
        history: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        """读取隐藏上下文检查点；可选地对原始历史前缀做完整性校验。"""
        checkpoint = read_context_checkpoint_file(self.context_checkpoint_path)
        if checkpoint is None:
            if self.context_checkpoint_path.exists():
                self.invalidate_context_checkpoint()
            return None
        if history is None:
            return checkpoint
        valid, reason = validate_context_checkpoint(checkpoint, history)
        if valid:
            return checkpoint
        logger.info(
            "[CONTEXT_CHECKPOINT] invalidate session=%s reason=%s",
            self.id,
            reason,
        )
        self.invalidate_context_checkpoint()
        return None

    def save_context_checkpoint(self, checkpoint: Dict[str, Any]) -> None:
        """原子保存派生检查点，不修改 transcript。"""
        write_context_checkpoint_file(self.context_checkpoint_path, checkpoint)

    def invalidate_context_checkpoint(self) -> None:
        """删除派生检查点；原始聊天历史始终保留。"""
        remove_context_checkpoint_file(self.context_checkpoint_path)

    def to_meta_dict(self) -> Dict:
        """导出为可持久化的元数据字典"""
        self._sync_metadata_mirror()
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "session_type": self.session_type,
            "root_session_id": self.root_session_id,
            "parent_session_id": self.parent_session_id,
            "owner_user_id": self.owner_user_id,
            "owner_type": self.owner_type,
            "owner_id": self.owner_id,
            "collab_type": self.collab_type,
            "scene_id": self.scene_id,
            "scene_type": self.scene_type,
            "participants": list(self.participants),
            "derived_from": self.derived_from,
            "status": self.status,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_active_at": self.last_active_at,
            "message_count": self.message_count,
            "history_revision": self.history_revision,
            "next_message_seq": self.next_message_seq,
            "next_event_seq": self.next_event_seq,
            "model": self.model,
            "provider": self.provider,
            "metadata": self.metadata,
            "task_description": self.task_description,
            "token_usage": dict(self.token_usage),
            "pinned_guideline": self.pinned_guideline,
            "pinned_scope": list(self.pinned_scope),
        }

    @classmethod
    def from_meta_dict(cls, data: Dict, session_dir: Path = None) -> 'Session':
        """从元数据字典重建 Session（兼容老文件：没有顶层字段时从 metadata 回填）"""
        meta = data.get("metadata") or {}
        collab_type = data.get("collab_type")
        if collab_type is None:
            collab_type = meta.get("collab_type")
        scene_id = data.get("scene_id")
        if scene_id is None:
            scene_id = meta.get("scene_id")
        scene_type = data.get("scene_type")
        if scene_type is None:
            scene_type = meta.get("scene_type")
        participants = data.get("participants")
        if not participants:
            participants = meta.get("participants") or []
        derived_from = data.get("derived_from")
        if derived_from is None:
            derived_from = meta.get("derived_from")

        # owner_type/owner_id 兼容老文件：缺失时按 collab_type 推断
        owner_type = data.get("owner_type")
        owner_id = data.get("owner_id")
        if not owner_type:
            owner_type = "room" if collab_type == "scene" else "agent"
        if not owner_id:
            owner_id = data["id"] if owner_type == "room" else data.get("agent_id", "")

        session = cls(
            id=data["id"],
            agent_id=data["agent_id"],
            session_type=data.get("session_type", "main"),
            root_session_id=data.get("root_session_id"),
            parent_session_id=data.get("parent_session_id"),
            owner_user_id=data.get("owner_user_id", "default"),
            owner_type=owner_type,
            owner_id=owner_id,
            collab_type=collab_type,
            scene_id=scene_id,
            scene_type=scene_type,
            participants=list(participants) if isinstance(participants, list) else [],
            derived_from=derived_from,
            status=data.get("status", "active"),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            last_active_at=data.get("last_active_at", ""),
            title=data.get("title", "新对话"),
            message_count=data.get("message_count", 0),
            history_revision=max(0, data.get("history_revision", 0)) if isinstance(data.get("history_revision", 0), int) and not isinstance(data.get("history_revision", 0), bool) else 0,
            next_message_seq=max(1, data.get("next_message_seq", 1)) if isinstance(data.get("next_message_seq", 1), int) and not isinstance(data.get("next_message_seq", 1), bool) else 1,
            next_event_seq=max(1, data.get("next_event_seq", 1)) if isinstance(data.get("next_event_seq", 1), int) and not isinstance(data.get("next_event_seq", 1), bool) else 1,
            model=data.get("model"),
            provider=data.get("provider"),
            metadata=meta,
            task_description=data.get("task_description"),
        )
        # 老 meta 文件兼容：缺 token_usage 字段时保留构造默认 0 dict
        raw_usage = data.get("token_usage")
        if isinstance(raw_usage, dict):
            session.token_usage = {
                "input": int(raw_usage.get("input", 0) or 0),
                "output": int(raw_usage.get("output", 0) or 0),
                "cache_read": int(raw_usage.get("cache_read", 0) or 0),
                "cache_create": int(raw_usage.get("cache_create", 0) or 0),
                "total": int(raw_usage.get("total", 0) or 0),
                "calls": int(raw_usage.get("calls", 0) or 0),
                "last_call_at": raw_usage.get("last_call_at"),
            }
        # Pinned context：老 meta 没此字段 → 留空；非法条目由 parse_scope_items 跳过
        raw_guideline = data.get("pinned_guideline")
        if isinstance(raw_guideline, str) and raw_guideline.strip():
            session.pinned_guideline = raw_guideline
        raw_scope = data.get("pinned_scope")
        session.pinned_scope = dump_scope_items(parse_scope_items(raw_scope))
        if session_dir:
            session.session_dir = session_dir

        # Lazy migration：老 session.json 只有 model 没 provider（2026-05-12 之前）。
        # 此处反查一次 providers store 把 provider 补上，避免前端 hydrate picker 时
        # 按 model id 反查走错家（同名 model 在多个 provider 下合法）。
        # 反查不到时静默保持 None；前端 fallback 会再 find 一次兜底。
        # 不在此处 save_meta（避免 from_meta_dict 做 IO）——下次 chat handler 触发
        # save_meta 时会带上反查到的 provider 一起落盘。
        if session.model and not session.provider:
            try:
                from backend.providers.store import find_model, load_providers_config_cached
                pc = find_model(load_providers_config_cached(), session.model, None)
                if pc is not None:
                    session.provider = pc.id
            except Exception:
                pass  # 反查失败静默降级；前端有 find fallback

        return session

    def load_conversation_history(self) -> List[Dict]:
        """从 transcript.jsonl 加载对话历史"""
        reader = TranscriptReader(self.transcript_path)
        reader.backfill_message_metadata()
        messages = reader.read_messages()
        self.conversation_history = reader.rebuild_conversation_history()
        self.message_count = len(messages)
        max_message_seq = max(
            (entry.get("message_seq", 0) for entry in self.conversation_history),
            default=0,
        )
        max_persisted_revision = max(
            (
                value
                for message in messages
                for value in [message.get("history_revision")]
                if isinstance(value, int) and not isinstance(value, bool) and value > 0
            ),
            default=0,
        )
        self.next_message_seq = max(self.next_message_seq, max_message_seq + 1)
        self.history_revision = max(
            self.history_revision,
            max_persisted_revision,
        )
        return self.conversation_history

    def _allocate_message_seq(self) -> int:
        message_seq = max(1, self.next_message_seq)
        self.next_message_seq = message_seq + 1
        return message_seq

    def _next_history_revision(self) -> int:
        return max(0, self.history_revision) + 1

    def load_emotion(self) -> Dict:
        """加载情感状态（优先从 session 目录，回退到默认）"""
        if self.emotion_path.exists():
            try:
                return json.loads(self.emotion_path.read_text(encoding='utf-8-sig'))
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        # 尝试从 transcript 恢复最后的情感状态
        reader = TranscriptReader(self.transcript_path)
        last_emotion = reader.get_last_emotion()
        if last_emotion:
            return {**DEFAULT_EMOTION, **last_emotion}
        return DEFAULT_EMOTION.copy()

    def save_emotion(self, emotion: Dict) -> None:
        """保存情感状态到 session 目录"""
        self.emotion_state = emotion
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.emotion_path.write_text(
            json.dumps(emotion, ensure_ascii=False, indent=2), encoding='utf-8'
        )

    def load_mask_state(self) -> Optional[Dict]:
        """加载面具状态"""
        if self.mask_state_path.exists():
            try:
                return json.loads(self.mask_state_path.read_text(encoding='utf-8-sig'))
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        # 尝试从 transcript 恢复
        reader = TranscriptReader(self.transcript_path)
        last_mask = reader.get_last_mask_state()
        if last_mask:
            return {**DEFAULT_MASK_STATE, **last_mask}
        return None

    def save_mask_state(self, mask_state: Dict) -> None:
        """保存面具状态到 session 目录"""
        self.mask_state = mask_state
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.mask_state_path.write_text(
            json.dumps(mask_state, ensure_ascii=False, indent=2), encoding='utf-8'
        )

    def save_meta(self) -> None:
        """保存会话元数据"""
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.session_meta_path.write_text(
            json.dumps(self.to_meta_dict(), ensure_ascii=False, indent=2), encoding='utf-8'
        )

    async def init_transcript_writer(self) -> TranscriptWriter:
        """初始化 transcript 写入器"""
        if self._transcript_writer is None:
            self._transcript_writer = TranscriptWriter(self.transcript_path)
        return self._transcript_writer

    async def append_message(
        self,
        role: str,
        content: str,
        timestamp: str = None,
        client_id: Optional[str] = None,
    ) -> None:
        """追加消息到 transcript。client_id 端到端追踪，前端 dedup 用。"""
        resolved_timestamp = timestamp or datetime.now().isoformat()
        message_seq = self._allocate_message_seq()
        history_revision = self._next_history_revision()
        writer = await self.init_transcript_writer()
        await writer.append_message(
            role,
            content,
            resolved_timestamp,
            client_id=client_id,
            message_seq=message_seq,
            history_revision=history_revision,
        )
        # 同步更新内存
        entry: Dict = {
            "role": role,
            "content": content,
            "timestamp": resolved_timestamp,
            "message_seq": message_seq,
            "history_revision": history_revision,
        }
        if client_id:
            entry["client_id"] = client_id
        self.conversation_history.append(entry)
        self.message_count += 1
        self.history_revision = history_revision
        self.updated_at = resolved_timestamp
        self.last_active_at = self.updated_at

    async def append_assistant_structured(
        self,
        segments: List[Dict],
        text: str,
        client_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> None:
        """以 Anthropic content blocks 格式追加 assistant 轮（v3）。

        参考 cline ClineStorageMessage 设计：内部存 Anthropic Messages API 格式，
        content 是有序 blocks 数组，天然保留 thinking/text/tool_use 的交错顺序。
        多个 tool_result 合并成下一条 user 消息（Anthropic 协议要求）。

        message_count 视作 1（这一轮 = 一个 assistant turn；user/tool_result
        消息是 assistant 工具调用的产物，不计入用户视角消息数）。
        """
        from .main import _segments_to_anthropic_blocks
        split = _segments_to_anthropic_blocks(segments)
        assistant_blocks: List[Dict[str, Any]] = list(split["assistant_blocks"])

        # 兼容：如果 segments 里没出现任何 text block 但调用方传了 text（最终回复）
        # —— 说明 LLM 只产出了 tool_use 没产 text，但我们想至少保留一个文本占位让
        # 前端能渲染。这种情况只补一个 text block 在末尾（保持顺序最直观）。
        has_text_block = any(b.get("type") == "text" for b in assistant_blocks)
        if not has_text_block and text:
            assistant_blocks.append({"type": "text", "text": text})

        resolved_ts = datetime.now().isoformat()
        writer = await self.init_transcript_writer()
        history_revision = self._next_history_revision()
        assistant_message_seq = self._allocate_message_seq()

        # 1. 写 assistant entry（content blocks 数组，保留交错顺序）
        await writer.append_assistant_blocks(
            content_blocks=assistant_blocks,
            timestamp=resolved_ts,
            client_id=client_id,
            agent_id=agent_id,
            message_seq=assistant_message_seq,
            history_revision=history_revision,
        )
        # 同步更新内存历史
        mem_entry: Dict[str, Any] = {
            "role": "assistant",
            "content": assistant_blocks,
            "timestamp": resolved_ts,
            "message_seq": assistant_message_seq,
            "history_revision": history_revision,
        }
        if client_id:
            mem_entry["client_id"] = client_id
        if agent_id:
            mem_entry["agent_id"] = agent_id
        self.conversation_history.append(mem_entry)
        self.message_count += 1
        self.history_revision = history_revision
        self.updated_at = resolved_ts
        self.last_active_at = self.updated_at

        # 2. 多条 tool_result 合并成下一条 user 消息（Anthropic 协议要求）
        if split["tool_results"]:
            tr_ts = datetime.now().isoformat()
            # Stage 1b：vision 跨轮持久化—— 写盘前把 image base64 抽出来按 sha256
            # 落到 agents/<id>/attachments/<sha>.png，content list 里替换为
            # image_ref（轻量引用）。conversation_history 内存里同样存 image_ref；
            # 真正喂给 LLM 时由 agent.build_messages reinflate 回 base64。
            # cline 流派 2 后 tool_result.content 一般是字符串（image 已被拆走），
            # persist_image_blocks_in_content 对字符串 content 是 no-op，安全调用。
            from .attachments import persist_image_blocks_in_content
            persisted_tool_results: List[Dict[str, Any]] = []
            for tr in split["tool_results"]:
                tr_content = tr.get("content")
                if isinstance(tr_content, list):
                    tr_content = persist_image_blocks_in_content(tr_content, self.agent_id)
                persisted_tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tr.get("tool_use_id") or "",
                    "content": tr_content if tr_content is not None else "",
                })
            tool_result_message_seq = self._allocate_message_seq()
            await writer.append_tool_results_user_message(
                tool_results=persisted_tool_results,
                timestamp=tr_ts,
                message_seq=tool_result_message_seq,
                history_revision=history_revision,
            )
            self.conversation_history.append({
                "role": "user",
                "content": persisted_tool_results,
                "timestamp": tr_ts,
                "message_seq": tool_result_message_seq,
                "history_revision": history_revision,
            })
            # tool_result 包装成 user 消息——按 cline 模式不增 message_count
            # （从用户视角这不是"用户发的消息"，是 assistant 工具调用的副产物）

        # 3. cline 流派 2：image attachment 独立成一条 user message 紧跟在
        # tool_results 之后写盘 + 进 conversation_history。模型回放时这条
        # message 在合法位置（user message.content）暴露真图，避开反代 tool_result
        # 吃图问题。事故根因 + 切派理由见 backend/llm_client.py:
        # _split_text_and_image_blocks docstring。
        image_msgs = split.get("image_user_messages") or []
        if image_msgs:
            from .attachments import persist_image_blocks_in_content
            for img_msg in image_msgs:
                img_blocks = img_msg.get("content_blocks") or []
                if not isinstance(img_blocks, list) or not img_blocks:
                    continue
                # 落盘前 image base64 → image_ref（per-agent 内容寻址）
                persisted_blocks = persist_image_blocks_in_content(img_blocks, self.agent_id)
                if not isinstance(persisted_blocks, list) or not persisted_blocks:
                    continue
                img_ts = datetime.now().isoformat()
                image_message_seq = self._allocate_message_seq()
                await writer.append_user_image_message(
                    content_blocks=persisted_blocks,
                    timestamp=img_ts,
                    message_seq=image_message_seq,
                    history_revision=history_revision,
                )
                self.conversation_history.append({
                    "role": "user",
                    "content": persisted_blocks,
                    "timestamp": img_ts,
                    "message_seq": image_message_seq,
                    "history_revision": history_revision,
                })
                # 同 tool_result：不增 message_count（assistant 工具调用副产物）

    async def close(self) -> None:
        """关闭 transcript 写入器"""
        if self._transcript_writer:
            await self._transcript_writer.close()
            self._transcript_writer = None

    async def edit_message(
        self,
        visible_index: int,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """编辑指定"用户视角"消息（v4 2026-05-22 引入，L2 聊天编辑）。

        薄包装 ``TranscriptWriter.edit_message`` —— 详细 payload 形态、可编辑
        字段、artifact 不可编辑规则等见 transcript.py docstring。

        修改成功后会：
        1. 从 transcript 重读 ``conversation_history``，保证内存与盘上一致；
        2. 更新 ``updated_at`` / ``last_active_at``；
        3. 落盘 session.json。

        Args:
            visible_index: 0-indexed 用户视角索引
            payload: 编辑负载（见 ``TranscriptWriter.edit_message``）

        Returns:
            transcript writer 的返回值 + ``conversation_history_len`` 字段。
        """
        writer = await self.init_transcript_writer()
        history_revision = self._next_history_revision()
        result = await writer.edit_message(
            visible_index,
            payload,
            history_revision=history_revision,
        )

        if not result.get("edited"):
            return result

        self.history_revision = history_revision
        # 重建内存历史（与 truncate_messages 同步策略）
        reader = TranscriptReader(self.transcript_path)
        self.conversation_history = reader.rebuild_conversation_history()
        now = datetime.now().isoformat()
        self.updated_at = now
        self.last_active_at = now
        self.invalidate_context_checkpoint()
        self.save_meta()

        result["conversation_history_len"] = len(self.conversation_history)
        return result

    async def delete_message(self, visible_index: int) -> Dict[str, Any]:
        """删除单条"用户视角"消息（v5 2026-05-26 引入，对齐 Cherry "选哪条删哪条"）。

        薄包装 ``TranscriptWriter.delete_visible_message`` —— 详见该方法 docstring：
        - 删 user 普通消息：删该行
        - 删 assistant turn：删该行 + 紧随其后的所有 tool_result / image artifact 行
        - emotion/mask/worker 等非 message 行原样保留
        - 永远不留孤儿 tool_use/tool_result 配对

        Args:
            visible_index: 0-indexed 用户视角索引

        Returns:
            transcript writer 的返回值 + ``conversation_history_len`` /
            ``message_count`` 字段（成功时）。
        """
        writer = await self.init_transcript_writer()
        result = await writer.delete_visible_message(visible_index)

        if not result.get("deleted"):
            return result

        self.history_revision = self._next_history_revision()
        # 重建内存历史 + message_count（与 truncate_messages 同策略）
        reader = TranscriptReader(self.transcript_path)
        self.conversation_history = reader.rebuild_conversation_history()
        self.message_count = len(reader.read_messages())
        now = datetime.now().isoformat()
        self.updated_at = now
        self.last_active_at = now
        self.invalidate_context_checkpoint()
        self.save_meta()

        result["conversation_history_len"] = len(self.conversation_history)
        result["message_count"] = self.message_count
        return result

    async def truncate_messages(self, keep_count: int) -> Dict[str, int]:
        """截断会话消息：保留前 ``keep_count`` 条用户视角 message，之后全部删除。

        语义见 ``TranscriptWriter.truncate_after`` 的 docstring：
        - 跟随某 assistant turn 的 ``tool_result`` / ``image attachment`` user
          message 不计入 keep_count，但**跟随保留**，避免下次 LLM 调用 tool_use
          没有 tool_result 配对 → 400 ``No tool output found``。
        - emotion/mask/worker 等非 message 行原样保留。

        截断后：
        1. 重新打开 writer（旧句柄已被 truncate_after close 掉）；下次 append
           走新句柄追加到截断后的末尾。
        2. 重建 ``conversation_history``（read once from transcript），保证内存
           和盘上完全一致。
        3. 重置 ``message_count``（按 transcript 实际 message 行数；与初次加载
           会话时口径一致）。
        4. 更新 ``updated_at`` / ``last_active_at``，落盘 session.json。

        Args:
            keep_count: 保留的用户视角消息数。0 = 清空（但 transcript 上的
                emotion/mask 行仍保留）。

        Returns:
            ``{"removed_lines": N, "kept_visible": K, "message_count": M}``
        """
        writer = await self.init_transcript_writer()
        result = await writer.truncate_after(keep_count)
        # truncate_after 自己已经 close 了 append 句柄，但 self._transcript_writer
        # 引用还在；为下次 append 显式清空，让 init_transcript_writer 重新创建
        # （或者复用对象——它的 _ensure_open 会自动 reopen，所以这里其实不需要
        # 重置，留意保持原对象）
        # 注：这里**不要** self._transcript_writer = None，否则会丢掉 _lock
        # 状态——_ensure_open 会按需 reopen file handle，对象本身可复用。

        if result["removed_lines"] == 0:
            return {
                "removed_lines": 0,
                "kept_visible": result["kept_visible"],
                "message_count": self.message_count,
            }

        self.history_revision = self._next_history_revision()
        # 重建内存历史：从 transcript 重读，保证与盘一致
        reader = TranscriptReader(self.transcript_path)
        self.conversation_history = reader.rebuild_conversation_history()
        self.message_count = len(reader.read_messages())
        now = datetime.now().isoformat()
        self.updated_at = now
        self.last_active_at = now
        self.invalidate_context_checkpoint()
        self.save_meta()

        return {
            "removed_lines": result["removed_lines"],
            "kept_visible": result["kept_visible"],
            "message_count": self.message_count,
        }

    # ---- Pinned context API ----

    def get_pinned_scope_items(self) -> List[ScopeItem]:
        """把持久化的 dict 列表解析为 ScopeItem 对象列表（非法条目自动跳过）。"""
        return parse_scope_items(self.pinned_scope)

    def set_pinned_guideline(self, guideline: Optional[str]) -> None:
        """设置/清空会话级 guideline。空串 / None 视作清空。"""
        if guideline is None or not guideline.strip():
            self.pinned_guideline = None
        else:
            self.pinned_guideline = guideline
        self.updated_at = datetime.now().isoformat()
        self.save_meta()

    def set_pinned_scope(self, items: List[Any]) -> List[Dict[str, Any]]:
        """覆盖式设置 pinned_scope。入参可以是 ScopeItem 列表或 dict 列表，统一规范化后写入。

        返回最终落库的 dict 列表（非法条目已被丢弃）。
        """
        normalized: List[Dict[str, Any]] = []
        for entry in items or []:
            if isinstance(entry, ScopeItem):
                normalized.append(entry.to_dict())
                continue
            if isinstance(entry, dict):
                try:
                    normalized.append(ScopeItem.from_dict(entry).to_dict())
                except Exception:
                    continue
        self.pinned_scope = normalized
        self.updated_at = datetime.now().isoformat()
        self.save_meta()
        return list(self.pinned_scope)

    def clear_pinned(self) -> None:
        """一键清空 guideline + scope。"""
        self.pinned_guideline = None
        self.pinned_scope = []
        self.updated_at = datetime.now().isoformat()
        self.save_meta()

    def add_token_usage(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read: int = 0,
        cache_create: int = 0,
    ) -> Dict:
        """纯累加。返回累加后的 token_usage dict 副本（写盘 / SSE 由编排层负责）。"""
        if not isinstance(self.token_usage, dict):
            self.token_usage = {
                "input": 0, "output": 0,
                "cache_read": 0, "cache_create": 0,
                "total": 0, "calls": 0, "last_call_at": None,
            }
        delta_input = max(int(input_tokens or 0), 0)
        delta_output = max(int(output_tokens or 0), 0)
        delta_cache_read = max(int(cache_read or 0), 0)
        delta_cache_create = max(int(cache_create or 0), 0)
        self.token_usage["input"] = int(self.token_usage.get("input", 0) or 0) + delta_input
        self.token_usage["output"] = int(self.token_usage.get("output", 0) or 0) + delta_output
        self.token_usage["cache_read"] = int(self.token_usage.get("cache_read", 0) or 0) + delta_cache_read
        self.token_usage["cache_create"] = int(self.token_usage.get("cache_create", 0) or 0) + delta_cache_create
        self.token_usage["total"] = (
            self.token_usage["input"]
            + self.token_usage["output"]
            + self.token_usage["cache_read"]
            + self.token_usage["cache_create"]
        )
        self.token_usage["calls"] = int(self.token_usage.get("calls", 0) or 0) + 1
        self.token_usage["last_call_at"] = datetime.now().isoformat()
        return dict(self.token_usage)


# ====== AgentProfile 共享层 ======
class AgentProfile:
    """角色共享层 — 同角色所有 MainSession 共享读取"""

    def __init__(self, agent_id: str, agent_dir: Path):
        self.agent_id = agent_id
        self.agent_dir = agent_dir
        self._profile_lock = asyncio.Lock()
        self.active_sessions: Dict[str, Session] = {}
        self.session_order: List[str] = []

    # ---- 文件读取（不加锁，文件系统保证原子读）----

    def read_genome(self) -> str:
        return self._read_agent_file("GENOME.md")

    def read_identity(self) -> str:
        return self._read_agent_file("IDENTITY.md")

    def read_soul(self) -> str:
        return self._read_agent_file("SOUL.md")

    def read_reflections(self) -> str:
        return self._read_agent_file("REFLECTIONS.md")

    def read_memory(self) -> str:
        return self._read_agent_file("MEMORY.md")

    def read_meta(self) -> Dict:
        path = self.agent_dir / "meta.json"
        if path.exists():
            try:
                return json.loads(path.read_text(encoding='utf-8-sig'))
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        return {}

    def _read_agent_file(self, filename: str) -> str:
        path = self.agent_dir / filename
        if path.exists():
            try:
                return path.read_text(encoding='utf-8-sig')
            except UnicodeDecodeError:
                try:
                    return path.read_text(encoding='utf-8')
                except (OSError, UnicodeDecodeError):
                    return ""
        return ""

    # ---- 文件写入（加锁）----

    async def write_shared(self, filename: str, content: str) -> None:
        """写入共享层文件（加 profile_lock）"""
        async with self._profile_lock:
            path = self.agent_dir / filename
            path.write_text(content, encoding='utf-8')

    # ---- 会话管理 ----

    def register_session(self, session: Session) -> None:
        """注册会话到 Profile"""
        self.active_sessions[session.id] = session
        if session.id not in self.session_order:
            self.session_order.append(session.id)

    def unregister_session(self, session_id: str) -> None:
        """注销会话"""
        self.active_sessions.pop(session_id, None)
        if session_id in self.session_order:
            self.session_order.remove(session_id)

    def get_peer_awareness(self, exclude_session_id: str) -> List[Dict]:
        """获取同角色其他会话的感知信息（不含对话内容）"""
        result = []
        for s in self.active_sessions.values():
            if s.id != exclude_session_id and s.session_type == "main":
                result.append({
                    "session_id": s.id,
                    "status": s.status,
                    "title": s.title,
                    "last_active_at": s.last_active_at,
                    "emotion_label": s.emotion_state.get("current", "平静") if s.emotion_state else "平静",
                })
        return result

    def get_session_list(self) -> List[Dict]:
        """获取按顺序排列的会话摘要列表"""
        result = []
        for sid in self.session_order:
            s = self.active_sessions.get(sid)
            if s:
                result.append({
                    "id": s.id,
                    "title": s.title,
                    "agent_id": s.agent_id,
                    "status": s.status,
                    "message_count": s.message_count,
                    "created_at": s.created_at,
                    "updated_at": s.updated_at,
                })
        return result


# ====== SessionManager 全局管理器 ======
class SessionManager:
    """会话生命周期管理器 — 全局唯一"""

    def __init__(self, base_dir: Path = None, agents_dir: Path = None):
        self._base_dir = (base_dir or DEFAULT_SESSION_BASE_DIR).resolve()
        self._sessions: Dict[str, Session] = {}
        self._profiles: Dict[str, AgentProfile] = {}
        self._session_locks: Dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()
        self._agents_dir = (agents_dir or DEFAULT_AGENTS_DIR).resolve()
        self._invalid_sessions: Dict[str, Dict] = {}

    def _get_session_dir(self, session_id: str) -> Path:
        """返回 session 当前应落地的目录。

        - 已知 session：按 owner_type + session_type 分派
          - worker: `<root.session_dir>/workers/<sid>`
          - room:   `council/rooms/<owner_id>/sessions/<sid>`
          - agent:  `agents/<agent_id>/sessions/<sid>`
        - 未知 session：两路 probe（agents/* → council/rooms/*），
          都没命中时返回 base_dir 兜底路径（新建写入的临时位置）
        """
        s = self._sessions.get(session_id)
        if s is not None:
            return self._resolve_session_dir_for(s)

        probed = self._probe_session_location(session_id)
        if probed is not None:
            return probed

        return self._base_dir / session_id

    def _resolve_session_dir_for(self, session: Session) -> Path:
        """根据 session 自身的 owner_type/session_type 算出应落地的目录"""
        # worker：挂在 root main 的 workers/ 下；root 在内存中时按其 owner 递归算
        if session.session_type == "worker":
            root_id = session.root_session_id or session.parent_session_id or session.id
            root = self._sessions.get(root_id)
            if root is not None and root.id != session.id:
                root_dir = self._resolve_session_dir_for(root)
            else:
                # root 还没加载到内存时退化为 probe
                probed_root = self._probe_session_location(root_id) if root_id and root_id != session.id else None
                root_dir = probed_root or (self._base_dir / root_id if root_id else self._base_dir / session.id)
            return root_dir / "workers" / session.id

        if session.owner_type == "room":
            room_id = session.owner_id or session.id
            return paths.council_room_session_dir(room_id, session.id)

        # 默认：单角色归属
        owner = session.owner_id or session.agent_id
        return paths.agent_session_dir(owner, session.id)

    def _probe_session_location(self, session_id: str) -> Optional[Path]:
        """两路扫盘定位 session 的物理目录。命中返回路径，不存在返回 None。"""
        # 1. agents/<aid>/sessions/<sid>/
        try:
            for agent_dir in self._agents_dir.iterdir():
                if not agent_dir.is_dir():
                    continue
                candidate = agent_dir / "sessions" / session_id
                if (candidate / "session.json").exists():
                    return candidate
                # 兼容 worker 嵌套：agents/<aid>/sessions/<main>/workers/<wid>/
                sessions_dir = agent_dir / "sessions"
                if sessions_dir.exists():
                    for main_dir in sessions_dir.iterdir():
                        if not main_dir.is_dir():
                            continue
                        worker_candidate = main_dir / "workers" / session_id
                        if (worker_candidate / "session.json").exists():
                            return worker_candidate
        except (OSError, FileNotFoundError):
            pass

        # 2. council/rooms/<rid>/sessions/<sid>/
        try:
            rooms_dir = paths.council_rooms_dir()
            for room_dir in rooms_dir.iterdir():
                if not room_dir.is_dir():
                    continue
                candidate = room_dir / "sessions" / session_id
                if (candidate / "session.json").exists():
                    return candidate
        except (OSError, FileNotFoundError):
            pass

        return None

    def _ensure_room_meta(
        self,
        room_id: str,
        collab_type: Optional[str],
        scene_id: Optional[str],
        participants: List[str],
        session_id: str,
        derived_from: Optional[str] = None,
    ) -> None:
        """为议事厅 room 兜底写 meta.json。已存在则合并 sessions 列表，其他字段不覆盖。"""
        room_dir = paths.council_room_dir(room_id)
        meta_path = room_dir / "meta.json"
        now = datetime.now().isoformat()

        if meta_path.exists():
            try:
                existing = json.loads(meta_path.read_text(encoding="utf-8-sig"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                existing = {}
            existing_sessions = existing.get("sessions") or []
            if session_id not in existing_sessions:
                existing_sessions.append(session_id)
            existing["sessions"] = existing_sessions
            existing.setdefault("rid", room_id)
            existing.setdefault("created_at", now)
            existing.setdefault("collab_type", collab_type)
            existing.setdefault("scene_id", scene_id)
            if participants and not existing.get("participants"):
                existing["participants"] = list(participants)
            if derived_from and not existing.get("derived_from"):
                existing["derived_from"] = derived_from
            meta_path.write_text(
                json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return

        meta = {
            "rid": room_id,
            "created_at": now,
            "collab_type": collab_type,
            "scene_id": scene_id,
            "participants": list(participants),
            "sessions": [session_id],
            "derived_from": derived_from,
        }
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        if session_id not in self._session_locks:
            self._session_locks[session_id] = asyncio.Lock()
        return self._session_locks[session_id]

    def _get_profile(self, agent_id: str) -> AgentProfile:
        if agent_id not in self._profiles:
            agent_dir = self._agents_dir / agent_id
            self._profiles[agent_id] = AgentProfile(agent_id, agent_dir)
        return self._profiles[agent_id]

    def _normalize_agent_id(self, agent_id: Optional[str]) -> Optional[str]:
        if not isinstance(agent_id, str):
            return None
        normalized = agent_id.strip()
        return normalized or None

    def _is_valid_agent_id(self, agent_id: Optional[str]) -> bool:
        normalized = self._normalize_agent_id(agent_id)
        if not normalized:
            return False
        if normalized == "default":
            return True
        return (self._agents_dir / normalized).exists()

    def _record_invalid_session(self, session_id: str, reason: str, session: Optional[Session] = None) -> None:
        invalid = {
            "session_id": session_id,
            "reason": reason,
        }
        if session:
            invalid["agent_id"] = session.agent_id
            invalid["title"] = session.title
            if session.metadata:
                invalid["metadata"] = session.metadata
        self._invalid_sessions[session_id] = invalid

    def _clear_invalid_session(self, session_id: str) -> None:
        self._invalid_sessions.pop(session_id, None)

    def get_invalid_session(self, session_id: str) -> Optional[Dict]:
        return self._invalid_sessions.get(session_id)

    def get_invalid_session_summary(self) -> Dict:
        invalid_sessions = list(self._invalid_sessions.values())
        invalid_sessions.sort(key=lambda item: item.get("session_id", ""))
        return {
            "invalid_session_count": len(invalid_sessions),
            "invalid_sessions": invalid_sessions,
        }

    def _repair_session_ownership(self, session: Session) -> Optional[str]:
        normalized_agent_id = self._normalize_agent_id(session.agent_id)

        # 路径即事实：如果 session 物理落在 agents/<X>/sessions/ 下，agent_id 以 X 为准
        try:
            session_dir = session._session_dir
            if session_dir is not None:
                parts = session_dir.resolve().parts
                if "agents" in parts and "sessions" in parts:
                    idx = parts.index("agents")
                    # agents/<agent_id>/sessions/<sid>
                    if idx + 2 < len(parts) and parts[idx + 2] == "sessions":
                        path_agent = parts[idx + 1]
                        if self._is_valid_agent_id(path_agent):
                            if session.agent_id != path_agent:
                                logger.warning("[SESSION] repair by path: %s agent_id %s -> %s", session.id, session.agent_id, path_agent)
                            session.agent_id = path_agent
                            if session.owner_type == "agent" and session.owner_id != path_agent:
                                session.owner_id = path_agent
                            return None
                if "council" in parts and "rooms" in parts:
                    # council/rooms/<rid>/sessions/<sid>
                    idx = parts.index("rooms")
                    if idx + 1 < len(parts):
                        rid = parts[idx + 1]
                        session.owner_type = "room"
                        session.owner_id = rid
        except Exception:
            pass

        if self._is_valid_agent_id(normalized_agent_id):
            if normalized_agent_id != session.agent_id:
                session.agent_id = normalized_agent_id
            return None

        repair_candidates: List[str] = []
        if session.id == "default":
            repair_candidates.append("default")

        metadata = session.metadata if isinstance(session.metadata, dict) else {}
        for key in ("owner_agent_id", "agent_id", "session_agent_id"):
            candidate = self._normalize_agent_id(metadata.get(key))
            if candidate and candidate not in repair_candidates:
                repair_candidates.append(candidate)

        for candidate in repair_candidates:
            if self._is_valid_agent_id(candidate):
                session.agent_id = candidate
                return None

        if not normalized_agent_id:
            return "missing session owner"
        return f"invalid session owner: {normalized_agent_id}"

    # ---- 会话 CRUD ----

    async def create_session(
        self,
        agent_id: str,
        title: str = "新对话",
        model: str = None,
        provider: Optional[str] = None,
        owner_user_id: str = "default",
        session_id: Optional[str] = None,
        # 以下新增参数用于 worker / 议事厅 / 协作会话一次性建齐
        session_type: str = "main",
        parent_session_id: Optional[str] = None,
        root_session_id: Optional[str] = None,
        task_description: Optional[str] = None,
        owner_type: Optional[str] = None,
        owner_id: Optional[str] = None,
        collab_type: Optional[str] = None,
        scene_id: Optional[str] = None,
        scene_type: Optional[str] = None,
        participants: Optional[List[str]] = None,
        derived_from: Optional[str] = None,
    ) -> Session:
        """创建新会话

        - owner_type 未指定时：协作会话（collab_type 有值）落议事厅 room，其他走 agent
        - session_id 未指定时：worker 自动用 `w-<hex8>`，其他用 uuid[:8]
        """
        normalized_agent_id = self._normalize_agent_id(agent_id)
        if not self._is_valid_agent_id(normalized_agent_id):
            raise ValueError(f"invalid agent_id: {agent_id}")

        # session_id 分配规则
        if not session_id:
            if session_type == "worker":
                session_id = f"w-{uuid.uuid4().hex[:8]}"
            else:
                session_id = str(uuid.uuid4())[:8]
        self._clear_invalid_session(session_id)

        # owner 解析：显式参数优先，否则按协作类型推断
        resolved_owner_type = owner_type or ("room" if collab_type == "scene" else "agent")
        if resolved_owner_type == "room":
            resolved_owner_id = owner_id or session_id  # 本轮 rid=sid
        else:
            resolved_owner_id = owner_id or normalized_agent_id

        session = Session(
            id=session_id,
            agent_id=normalized_agent_id,
            session_type=session_type,
            root_session_id=root_session_id or session_id,
            parent_session_id=parent_session_id,
            owner_user_id=owner_user_id,
            owner_type=resolved_owner_type,
            owner_id=resolved_owner_id,
            collab_type=collab_type,
            scene_id=scene_id,
            scene_type=scene_type,
            participants=list(participants) if participants else [],
            derived_from=derived_from,
            title=title,
            model=model,
            provider=provider,
            task_description=task_description,
        )

        # 计算并创建终点目录（一次落位，避免两阶段写盘）
        session_dir = self._resolve_session_dir_for(session)
        session.session_dir = session_dir
        session_dir.mkdir(parents=True, exist_ok=True)

        # 议事厅 room 的 meta.json 兜底创建
        if resolved_owner_type == "room":
            self._ensure_room_meta(
                resolved_owner_id,
                collab_type=collab_type,
                scene_id=scene_id,
                participants=list(participants) if participants else [],
                session_id=session_id,
                derived_from=derived_from,
            )

        # 初始化情感状态（从 Agent 的 EMOTION.json 复制，如果有）
        agent_emotion_path = self._agents_dir / normalized_agent_id / "EMOTION.json"
        if agent_emotion_path.exists():
            try:
                emotion = json.loads(agent_emotion_path.read_text(encoding='utf-8-sig'))
                session.save_emotion(emotion)
                session.emotion_state = emotion
            except (json.JSONDecodeError, UnicodeDecodeError):
                session.save_emotion(DEFAULT_EMOTION.copy())
        else:
            session.save_emotion(DEFAULT_EMOTION.copy())

        # 保存元数据
        session.save_meta()

        # 注册到内存
        self._sessions[session_id] = session
        self._get_profile(normalized_agent_id).register_session(session)

        return session

    async def get_session(self, session_id: str) -> Optional[Session]:
        """获取会话（优先内存，回退磁盘）"""
        if session_id in self._sessions:
            return self._sessions[session_id]

        # 尝试从磁盘恢复
        return await self._recover_session(session_id)

    async def _recover_session(self, session_id: str) -> Optional[Session]:
        """从磁盘恢复单个会话"""
        # 两路 probe 定位真实位置（agents/* 或 council/rooms/*）
        session_dir = self._probe_session_location(session_id) or self._get_session_dir(session_id)

        # 新格式：{id}/session.json
        meta_path = session_dir / "session.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding='utf-8-sig'))
                session = Session.from_meta_dict(meta, session_dir=session_dir)
                repair_reason = self._repair_session_ownership(session)
                if repair_reason:
                    self._record_invalid_session(session_id, repair_reason, session)
                    return None

                session.load_conversation_history()
                session.emotion_state = session.load_emotion()
                session.mask_state = session.load_mask_state()
                session.save_meta()
                self._clear_invalid_session(session_id)

                # 注册到内存
                self._sessions[session_id] = session
                self._get_profile(session.agent_id).register_session(session)
                return session
            except Exception as e:
                logger.warning("[SESSION] Failed to recover session %s: %s", session_id, e)
                self._record_invalid_session(session_id, f"recover failed: {e}")
                return None

        return None

    async def list_sessions(self, agent_id: str = None) -> List[Dict]:
        """列出所有会话（支持按 agent_id 过滤）"""
        sessions = []
        normalized_agent_id = self._normalize_agent_id(agent_id)
        await self._load_all_sessions()

        for session in self._sessions.values():
            if normalized_agent_id and session.agent_id != normalized_agent_id:
                continue
            if session.session_type != "main":
                continue
            sessions.append({
                "id": session.id,
                "title": session.title,
                "agent_id": session.agent_id,
                "status": session.status,
                # camelCase 跟前端 Session 类型对齐（其它端点 create_session /
                # get_session 都用 camelCase）。这里历史上写成 snake_case 导致
                # 前端 session.updatedAt / messageCount === undefined，
                # RoundtableList 卡片"X 分钟前"显示空、排序退化为 0。
                "messageCount": session.message_count,
                "createdAt": session.created_at,
                "updatedAt": session.updated_at,
                "metadata": session.metadata or {},
                # token_usage 跟随列表一起回，前端 hydrate store 用，避免重启后
                # badge 在用户发新消息触发 SSE 之前一直显示 "—"
                "token_usage": dict(session.token_usage) if isinstance(session.token_usage, dict) else None,
                # per-session model override：前端 ChatModelPicker 切模型时写
                # session.model，切会话时靠此字段回显本会话的 override（为空
                # 表示该会话跟随 agent.active_model / global current_model）
                "model": session.model,
                # 与 model 配对的 provider id。仅 model 不够定位（同名 model 在多个
                # provider 下合法存在），前端 hydrate picker 需要 provider 才能精准匹配。
                "provider": session.provider,
            })

        sessions.sort(key=lambda x: x.get("updatedAt", ""), reverse=True)
        return sessions

    async def delete_session(self, session_id: str) -> bool:
        """删除会话（main session 会级联清掉所有 worker 子会话的内存索引）"""
        session = await self.get_session(session_id)
        invalid_session = None
        if not session:
            invalid_session = self.get_invalid_session(session_id)
            if not invalid_session:
                return False

        # 级联：先把依赖本 session 的 worker 从内存清掉（物理上 workers/ 子树随 rmtree 一起走）
        cascaded = 0
        if session and session.session_type == "main":
            cascaded = await self._cascade_delete_workers(session_id)

        # 从内存移除
        self._sessions.pop(session_id, None)
        self._session_locks.pop(session_id, None)
        if session:
            self._get_profile(session.agent_id).unregister_session(session_id)

            # 关闭写入器（释放文件锁）
            await session.close()

        self._clear_invalid_session(session_id)

        # 删除磁盘目录
        session_dir = self._get_session_dir(session_id)
        delete_error = None
        if session_dir.exists():
            try:
                shutil.rmtree(session_dir)
            except Exception as e:
                delete_error = e
                logger.warning("[SESSION] Failed to delete session dir %s: %s", session_dir, e)

        if session_dir.exists():
            if delete_error:
                raise OSError(f"failed to delete session storage for {session_id}: {delete_error}")
            raise OSError(f"failed to delete session storage for {session_id}")

        if cascaded:
            logger.info("[SESSION] cascaded %s worker sessions for %s", cascaded, session_id)

        return True

    async def _cascade_delete_workers(self, parent_id: str) -> int:
        """清理所有以 parent_id 为父/根的 worker 的内存索引。物理目录已嵌在 parent
        的 workers/ 子树中，由调用方的 rmtree 带走。"""
        await self._load_all_sessions()
        victims = [
            s for s in list(self._sessions.values())
            if s.session_type == "worker"
            and (s.parent_session_id == parent_id or s.root_session_id == parent_id)
        ]
        for w in victims:
            try:
                await w.close()
            except Exception:
                pass
            self._sessions.pop(w.id, None)
            self._session_locks.pop(w.id, None)
            if w.agent_id in self._profiles:
                self._profiles[w.agent_id].unregister_session(w.id)
        return len(victims)

    async def _evict_agent_sessions(self, agent_id: str) -> int:
        """角色被删除时清其所有 session 的内存索引。物理目录随 agent_dir rmtree 天然带走。"""
        normalized = self._normalize_agent_id(agent_id)
        if not normalized:
            return 0
        await self._load_all_sessions()
        victims = [s for s in list(self._sessions.values()) if s.agent_id == normalized]
        for s in victims:
            try:
                await s.close()
            except Exception:
                pass
            self._sessions.pop(s.id, None)
            self._session_locks.pop(s.id, None)
            self._clear_invalid_session(s.id)
        # 清 profile
        self._profiles.pop(normalized, None)
        return len(victims)

    async def clear_session_models_for_agent(self, agent_id: str) -> int:
        """清除该 agent 下所有 main session 的 per-session model override。

        语义：把 session.model 置 None 并落盘，让这些会话回落到 agent.active_model
        / global current_model。其他字段（token_usage / 消息 / metadata 等）一概不动。

        典型触发：用户在人设工坊改了角色默认模型 + 主动点了"重置所有会话"。
        worker 子会话不动（它们由 spawn 时的 task 决定模型，不应被批量清）。
        """
        normalized = self._normalize_agent_id(agent_id)
        if not normalized:
            return 0
        await self._load_all_sessions()
        cleared = 0
        for s in list(self._sessions.values()):
            if s.agent_id != normalized:
                continue
            if s.session_type != "main":
                continue
            if not s.model:
                continue
            s.model = None
            s.updated_at = datetime.now().isoformat()
            s.save_meta()
            cleared += 1
        return cleared

    async def update_title(self, session_id: str, title: str) -> Optional[Session]:
        """更新会话标题"""
        session = await self.get_session(session_id)
        if not session:
            return None
        session.title = title
        session.updated_at = datetime.now().isoformat()
        session.save_meta()
        return session

    # ---- 消息操作 ----

    async def load_context_checkpoint(
        self,
        session_id: str,
        history: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """读取并校验会话检查点；失效文件会被自动移除。"""
        session = await self.get_session(session_id)
        if not session:
            return None
        lock = self._get_session_lock(session_id)
        async with lock:
            return session.load_context_checkpoint(history)

    async def commit_context_checkpoint(
        self,
        session_id: str,
        checkpoint: Dict[str, Any],
        history: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """提交检查点，并发时优先复用覆盖范围更大或同源的稳定版本。

        同一个历史前缀若有两个并发摘要，保留先落盘的字节内容，避免下一轮
        Prompt Cache 因措辞随机波动再次失效。
        """
        valid, reason = validate_context_checkpoint(checkpoint, history)
        if not valid:
            logger.warning(
                "[CONTEXT_CHECKPOINT] reject session=%s reason=%s",
                session_id,
                reason,
            )
            return None

        session = await self.get_session(session_id)
        if not session:
            return None
        lock = self._get_session_lock(session_id)
        async with lock:
            existing = session.load_context_checkpoint(history)
            if existing is not None:
                old_count = int(existing.get("covered_message_count") or 0)
                new_count = int(checkpoint.get("covered_message_count") or 0)
                same_source = (
                    old_count == new_count
                    and existing.get("source_hash") == checkpoint.get("source_hash")
                    and existing.get("summary_prompt_version")
                    == checkpoint.get("summary_prompt_version")
                )
                if old_count > new_count or same_source:
                    return existing

            session.save_context_checkpoint(checkpoint)
            return checkpoint

    async def invalidate_context_checkpoint(self, session_id: str) -> None:
        session = await self.get_session(session_id)
        if not session:
            return
        lock = self._get_session_lock(session_id)
        async with lock:
            session.invalidate_context_checkpoint()

    async def reserve_event_sequences(
        self,
        session_id: str,
        count: int,
    ) -> Optional[tuple[int, int]]:
        session = await self.get_session(session_id)
        if not session:
            return None
        reservation_size = max(1, count)
        lock = self._get_session_lock(session_id)
        async with lock:
            start = max(1, session.next_event_seq)
            end = start + reservation_size
            session.next_event_seq = end
            session.save_meta()
            return start, end

    async def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        timestamp: str = None,
        client_id: Optional[str] = None,
    ) -> None:
        """追加消息到会话。client_id 透传给 Session.append_message。"""
        session = await self.get_session(session_id)
        if not session:
            return

        lock = self._get_session_lock(session_id)
        async with lock:
            await session.append_message(role, content, timestamp, client_id=client_id)
            # 自动更新标题（若当前标题为默认占位符，执行零延迟纯文本截断兜底）
            if role == "user" and is_default_session_title(session.title):
                text_content = ""
                if isinstance(content, str):
                    text_content = content
                elif isinstance(content, list):
                    text_content = "".join(
                        b.get("text", "") if isinstance(b, dict) else str(b)
                        for b in content
                    )
                clean_preview = re.sub(r"\s+", " ", text_content).strip()
                if clean_preview:
                    session.title = clean_preview[:24] + ("..." if len(clean_preview) > 24 else "")
            # 定期保存元数据
            session.save_meta()

    async def truncate_messages(
        self,
        session_id: str,
        keep_count: int,
    ) -> Optional[Dict[str, int]]:
        """截断会话消息（保留前 ``keep_count`` 条用户视角 message，之后全部删除）。

        语义详见 ``Session.truncate_messages``。复用 per-session 锁，与
        ``append_message`` / ``append_assistant_structured`` 互斥，避免截断
        和写入 race。

        ⚠️ 调用方应保证：本会话的 chat handler / streaming **已停止**
        （前端层面 streaming 中的消息不显示编辑/回滚按钮）。如果有 in-flight
        chat 在写盘，本方法会等它写完，但截断后该轮新增的消息会被截断到
        keep_count 之外——不会数据损坏，只是行为不直观。

        Args:
            session_id: 会话 id
            keep_count: 保留的用户视角消息数；< 0 自动按 0 处理

        Returns:
            截断结果 dict，会话不存在时返回 None。
        """
        session = await self.get_session(session_id)
        if not session:
            return None
        lock = self._get_session_lock(session_id)
        async with lock:
            return await session.truncate_messages(keep_count)

    async def edit_message(
        self,
        session_id: str,
        visible_index: int,
        payload: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """编辑指定"用户视角"消息（v4 2026-05-22 L2 聊天编辑）。

        薄包装 ``Session.edit_message`` —— 详见该方法 docstring。复用 per-session
        锁，与 ``append_*`` / ``truncate_messages`` 互斥。

        ⚠️ 调用方应保证：本会话的 chat handler / streaming **已停止**
        （前端层面 streaming 中的消息不显示编辑按钮）。如果有 in-flight chat
        在写盘，本方法会等它写完——不会数据损坏，但语义上"被编辑的可能不是
        你点击的那条"。

        Args:
            session_id: 会话 id
            visible_index: 0-indexed 用户视角索引
            payload: 编辑负载

        Returns:
            编辑结果 dict，会话不存在时返回 None。payload 形态非法会抛 ValueError
            （上层应转 400）。
        """
        session = await self.get_session(session_id)
        if not session:
            return None
        lock = self._get_session_lock(session_id)
        async with lock:
            return await session.edit_message(visible_index, payload)

    async def delete_message(
        self,
        session_id: str,
        visible_index: int,
    ) -> Optional[Dict[str, Any]]:
        """删除指定"用户视角"消息（v5 2026-05-26 单条删除，对齐 Cherry "选哪条删哪条"）。

        薄包装 ``Session.delete_message`` —— 详见该方法 docstring。复用 per-session
        锁，与 ``append_*`` / ``truncate_messages`` / ``edit_message`` 互斥。

        ⚠️ 调用方应保证：本会话的 chat handler / streaming **已停止**
        （前端层面 streaming 中的消息不显示删除按钮）。

        Args:
            session_id: 会话 id
            visible_index: 0-indexed 用户视角索引

        Returns:
            删除结果 dict，会话不存在时返回 None。
        """
        session = await self.get_session(session_id)
        if not session:
            return None
        lock = self._get_session_lock(session_id)
        async with lock:
            return await session.delete_message(visible_index)

    async def append_assistant_structured(
        self,
        session_id: str,
        segments: List[Dict],
        text: str,
        client_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> None:
        """OpenAI ChatCompletion 标准格式追加 assistant 轮（详见 Session.append_assistant_structured）。"""
        session = await self.get_session(session_id)
        if not session:
            return
        lock = self._get_session_lock(session_id)
        async with lock:
            await session.append_assistant_structured(
                segments=segments,
                text=text,
                client_id=client_id,
                agent_id=agent_id,
            )
            session.save_meta()

    # ---- Token 累计消耗 ----

    async def record_token_usage(
        self,
        session_id: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read: int = 0,
        cache_create: int = 0,
    ) -> Optional[Dict]:
        """LLM 调用层入口：累加 token usage、写盘 meta、发 SSE 事件。

        - session 不存在时静默返回 None（不抛错，避免污染 LLM 调用主流程）
        - 全 0 输入直接 no-op，避免空 calls 累加
        - SSE event type: `session_token_usage`，payload 是累加后的完整 dict
        """
        if not session_id:
            return None
        if not (input_tokens or output_tokens or cache_read or cache_create):
            return None
        session = await self.get_session(session_id)
        if not session:
            return None
        lock = self._get_session_lock(session_id)
        async with lock:
            usage = session.add_token_usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read=cache_read,
                cache_create=cache_create,
            )
            session.save_meta()
        try:
            from .sse_manager import get_sse_manager
            sse = get_sse_manager()
            await sse.send_event(
                session_id,
                {"type": "session_token_usage", "payload": dict(usage)},
            )
        except Exception:
            pass  # SSE 失败不影响累加 + 落盘

        # 追加到 prompt_cache_stats.jsonl 用于历史命中率分析
        # （session.meta 只存累加值，时间维度丢失；要对比优化前后需要原始增量）
        try:
            from .prompt_cache_stats import record_cache_stat
            await record_cache_stat(
                session_id=session_id,
                agent_id=session.agent_id,
                model=session.model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read=cache_read,
                cache_create=cache_create,
            )
        except Exception:
            pass  # 统计失败不影响主流程
        return usage

    # ---- 情感/面具状态 ----

    async def update_emotion(self, session_id: str, emotion: Dict) -> None:
        """更新会话情感状态"""
        session = await self.get_session(session_id)
        if not session:
            return
        session.save_emotion(emotion)
        # 同时写入 transcript
        writer = await session.init_transcript_writer()
        await writer.append_emotion(emotion)

    async def update_mask_state(self, session_id: str, mask_state: Dict) -> None:
        """更新会话面具状态"""
        session = await self.get_session(session_id)
        if not session:
            return
        session.save_mask_state(mask_state)
        writer = await session.init_transcript_writer()
        await writer.append_mask(mask_state)

    # ---- 跨会话感知 ----

    async def get_peer_awareness(self, session_id: str) -> List[Dict]:
        """获取同角色其他会话的感知信息"""
        session = await self.get_session(session_id)
        if not session:
            return []
        profile = self._get_profile(session.agent_id)
        return profile.get_peer_awareness(session_id)

    # ---- 启动恢复 ----

    async def recover_all_sessions(self) -> int:
        """启动时恢复所有会话，返回恢复数量"""
        count_before = len(self._sessions)
        await self._load_all_sessions()
        count = len(self._sessions) - count_before
        logger.info("[SESSION] Recovered %s sessions", count)
        return count

    async def _load_all_sessions(self) -> None:
        """扫两路归属结构加载所有磁盘会话：agents/<aid>/sessions/* 和 council/rooms/<rid>/sessions/*"""
        # 1. agents/<aid>/sessions/<sid>/
        if self._agents_dir.exists():
            for agent_dir in self._agents_dir.iterdir():
                if not agent_dir.is_dir():
                    continue
                sessions_dir = agent_dir / "sessions"
                if not sessions_dir.exists():
                    continue
                for sdir in sessions_dir.iterdir():
                    if not sdir.is_dir() or sdir.name in self._sessions:
                        continue
                    if (sdir / "session.json").exists():
                        await self._recover_session(sdir.name)
                    # 嵌套的 worker 子会话
                    workers_dir = sdir / "workers"
                    if workers_dir.exists():
                        for wdir in workers_dir.iterdir():
                            if not wdir.is_dir() or wdir.name in self._sessions:
                                continue
                            if (wdir / "session.json").exists():
                                await self._recover_session(wdir.name)

        # 2. council/rooms/<rid>/sessions/<sid>/
        rooms_dir = paths.council_rooms_dir()
        if rooms_dir.exists():
            for room_dir in rooms_dir.iterdir():
                if not room_dir.is_dir():
                    continue
                sessions_dir = room_dir / "sessions"
                if not sessions_dir.exists():
                    continue
                for sdir in sessions_dir.iterdir():
                    if not sdir.is_dir() or sdir.name in self._sessions:
                        continue
                    if (sdir / "session.json").exists():
                        await self._recover_session(sdir.name)


# ====== 全局单例 ======
_session_manager: Optional[SessionManager] = None


def get_session_manager() -> SessionManager:
    """获取全局 SessionManager 实例"""
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager()
    return _session_manager
