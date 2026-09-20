"""SessionKey：跨平台会话身份的统一 schema

格式：`{platform}:{chat_type}:{chat_id}[:{user_id}]`

兼容性：
- web 端的 chat_id 仍然是 UUID（兼容现有数据库）
- 所有 backend 内部传递的 sid 应该是 SessionKey 字符串形式
- 现有裸 UUID 通过 `from_legacy_uuid()` 包装

不变量：
- 字段不可变（frozen dataclass）
- chat_type 只允许 "dm" / "group"
- group 必须有 user_id（用于"是谁在群里发的"）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, FrozenSet, Optional


_VALID_CHAT_TYPES: FrozenSet[str] = frozenset({"dm", "group"})
_VALID_PLATFORMS: FrozenSet[str] = frozenset({
    "web", "wechat", "feishu", "telegram", "internal",
})


class SessionKeyError(ValueError):
    """SessionKey 解析或构造失败"""


@dataclass(frozen=True)
class SessionKey:
    platform: str
    chat_type: str
    chat_id: str
    user_id: Optional[str] = None

    SEPARATOR: ClassVar[str] = ":"

    def __post_init__(self) -> None:
        if not self.platform:
            raise SessionKeyError("platform is required")
        if self.chat_type not in _VALID_CHAT_TYPES:
            raise SessionKeyError(
                f"chat_type must be one of {sorted(_VALID_CHAT_TYPES)}, got {self.chat_type!r}"
            )
        if not self.chat_id:
            raise SessionKeyError("chat_id is required")
        if self.chat_type == "group" and not self.user_id:
            raise SessionKeyError("user_id is required for group chats")
        for value in (self.platform, self.chat_type, self.chat_id, self.user_id or ""):
            if self.SEPARATOR in value:
                raise SessionKeyError(
                    f"value must not contain {self.SEPARATOR!r}: {value!r}"
                )

    def to_str(self) -> str:
        parts = [self.platform, self.chat_type, self.chat_id]
        if self.user_id:
            parts.append(self.user_id)
        return self.SEPARATOR.join(parts)

    def __str__(self) -> str:
        return self.to_str()

    @classmethod
    def parse(cls, raw: str) -> "SessionKey":
        if not raw:
            raise SessionKeyError("empty session key")
        parts = raw.split(cls.SEPARATOR)
        if len(parts) == 3:
            platform, chat_type, chat_id = parts
            return cls(platform=platform, chat_type=chat_type, chat_id=chat_id)
        if len(parts) == 4:
            platform, chat_type, chat_id, user_id = parts
            return cls(
                platform=platform,
                chat_type=chat_type,
                chat_id=chat_id,
                user_id=user_id,
            )
        raise SessionKeyError(
            f"invalid session key format: {raw!r} (expected 3 or 4 parts)"
        )

    @classmethod
    def build(
        cls,
        *,
        platform: str = "web",
        chat_type: str = "dm",
        chat_id: str,
        user_id: Optional[str] = None,
    ) -> "SessionKey":
        return cls(
            platform=platform,
            chat_type=chat_type,
            chat_id=chat_id,
            user_id=user_id,
        )

    @classmethod
    def from_legacy_uuid(cls, uuid_str: str, *, platform: str = "web") -> "SessionKey":
        """把现有裸 UUID 包装成 SessionKey

        用于迁移期：现有数据库里的 session_id 是 8 字符截断 UUID，包装成
        web:dm:<uuid> 格式后跟新代码联通。
        """
        return cls(platform=platform, chat_type="dm", chat_id=uuid_str)

    def is_legacy_format(self) -> bool:
        """判断是否是从裸 UUID 包装来的（无 user_id 的 web dm）"""
        return self.platform == "web" and self.chat_type == "dm" and self.user_id is None
