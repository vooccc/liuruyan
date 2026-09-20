from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional


class BaseMemoryProvider:
    """记忆后端抽象层。

    默认实现仍然可以是本地文件；后续若切到 SQLite、索引库或远端服务，
    只需要替换 provider，而不必重写 MemoryManager 主链路。
    """

    def __init__(
        self,
        *,
        agent_id: Optional[str],
        base_dir: Path,
        memory_dir: Path,
        long_term_memory_file: Path,
        conversation_history_file: Path,
        reflections_file: Path,
    ) -> None:
        self.agent_id = agent_id
        self.base_dir = base_dir
        self.memory_dir = memory_dir
        self.long_term_memory_file = long_term_memory_file
        self.conversation_history_file = conversation_history_file
        self.reflections_file = reflections_file
        self.ensure_ready()

    def ensure_ready(self) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    def prefetch(self, **kwargs) -> Dict[str, Any]:
        return {}

    def sync_turn(
        self,
        *,
        user_message: str,
        assistant_response: str,
        session_id: Optional[str] = None,
        message_span: Optional[List[int]] = None,
        diary_entry: Optional[str] = None,
    ) -> None:
        if diary_entry:
            self.append_to_diary_entry(diary_entry)

    def on_pre_compress(self, history: List[Dict[str, str]]) -> List[Dict[str, str]]:
        return history

    def on_session_end(
        self,
        *,
        session_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        return None

    def get_diary_path(self, date: Optional[str] = None) -> Path:
        target = date or self._today_str()
        return self.memory_dir / f"{target}.md"

    def get_today_diary_path(self) -> Path:
        return self.get_diary_path()

    def list_diary_files(self) -> List[Path]:
        if not self.memory_dir.exists():
            return []
        return sorted(
            [
                path
                for path in self.memory_dir.glob("*.md")
                if path.stem[:4].isdigit()
            ],
            reverse=True,
        )

    def read_diary(self, date: Optional[str] = None) -> str:
        path = self.get_diary_path(date)
        if path.exists():
            return path.read_text(encoding="utf-8")
        return ""

    def write_diary(self, content: str, date: Optional[str] = None, append: bool = True) -> None:
        path = self.get_diary_path(date)
        if append and path.exists():
            existing = path.read_text(encoding="utf-8")
            content = existing + "\n\n" + content
        path.write_text(content, encoding="utf-8")

    def append_to_diary_entry(self, entry: str) -> None:
        self.write_diary(entry, append=True)

    def read_long_term_memory(self) -> str:
        if self.long_term_memory_file.exists():
            return self.long_term_memory_file.read_text(encoding="utf-8")
        return ""

    def write_long_term_memory(self, content: str) -> None:
        self.long_term_memory_file.write_text(content, encoding="utf-8")

    def read_reflections(self) -> str:
        if self.reflections_file.exists():
            return self.reflections_file.read_text(encoding="utf-8")
        return ""

    def write_reflections(self, content: str) -> None:
        self.reflections_file.write_text(content, encoding="utf-8")

    def save_conversation_history(self, history: List[Dict[str, str]]) -> None:
        self.conversation_history_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.conversation_history_file, "w", encoding="utf-8") as file_obj:
            json.dump(history, file_obj, ensure_ascii=False, indent=2)

    def load_conversation_history(self) -> List[Dict[str, str]]:
        if not self.conversation_history_file.exists():
            return []

        try:
            with open(self.conversation_history_file, "r", encoding="utf-8") as file_obj:
                data = json.load(file_obj)
        except (json.JSONDecodeError, OSError):
            return []

        return data if isinstance(data, list) else []

    def clear_conversation_history(self) -> None:
        if self.conversation_history_file.exists():
            self.conversation_history_file.unlink()

    def _today_str(self) -> str:
        from datetime import datetime

        return datetime.now().strftime("%Y-%m-%d")


class FileMemoryProvider(BaseMemoryProvider):
    """默认文件型记忆后端。"""


def build_file_memory_provider(
    *,
    agent_id: Optional[str] = None,
    base_dir: Optional[str] = None,
) -> FileMemoryProvider:
    from .agent_config import get_agent_manager

    if agent_id:
        agent_config = get_agent_manager().get_agent_config(agent_id)
        if agent_config:
            agent_config.ensure_dirs()
            return FileMemoryProvider(
                agent_id=agent_id,
                base_dir=agent_config.agent_dir,
                memory_dir=agent_config.memory_dir,
                long_term_memory_file=agent_config.long_term_memory_file,
                conversation_history_file=agent_config.conversation_history_file,
                reflections_file=agent_config.reflections_file,
            )

    if base_dir is not None:
        resolved_base = Path(base_dir)
    else:
        from . import paths
        resolved_base = paths.data_dir()
    memory_dir = resolved_base / "memory"
    return FileMemoryProvider(
        agent_id=agent_id,
        base_dir=resolved_base,
        memory_dir=memory_dir,
        long_term_memory_file=resolved_base / "MEMORY.md",
        conversation_history_file=memory_dir / "conversation_history.json",
        reflections_file=resolved_base / "REFLECTIONS.md",
    )
