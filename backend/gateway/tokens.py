"""Gateway 令牌仓库 + 配额限流。

数据落盘：``~/.liuruyan/gateway_tokens.json``：

    {
      "version": 2,
      "tokens": [
        {
          "id": "tok_xxxxxxxx",
          "name": "默认令牌",
          "token": "lry_gw_<urlsafe>",
          "created_at": "2026-05-18T...",
          "last_used_at": null,
          "disabled": false,
          "allow_models": [],
          "deny_models": [],
          "allow_ips": [],
          "quota": {"calls_per_minute": null, "calls_per_day": null,
                    "total_tokens_per_day": null}
        }
      ]
    }

设计抉择：
- **明文存储**。本地软件场景下 hash 是"安全表演"——攻击者能读这个文件，
  就已经能读 ``~/.claude/settings.json`` 里 installer 写下的明文，token 的 hash
  防护毫无意义。主流本地工具均采用同款本地存储设计。
- **0600 权限**（POSIX）。Windows 默认 user profile ACL 已经够用。
- **首次启动 bootstrap**：``bootstrap_if_empty()`` 在文件不存在时建一条"默认令牌"。
  用户清空数组后不再自动建——尊重用户意图。
- **配额计量**：内存滑动窗口；重启清零（本地网关可接受，配额是防呆不是防滥用）。
"""
from __future__ import annotations

import datetime
import ipaddress
import json
import logging
import os
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

from .. import paths

logger = logging.getLogger(__name__)

TOKEN_PREFIX = "lry_gw_"
DEFAULT_TOKEN_NAME = "默认令牌"
STORAGE_VERSION = 2


# ─────────────────────── 数据结构 ───────────────────────


@dataclass
class TokenQuota:
    calls_per_minute: Optional[int] = None
    calls_per_day: Optional[int] = None
    total_tokens_per_day: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "calls_per_minute": self.calls_per_minute,
            "calls_per_day": self.calls_per_day,
            "total_tokens_per_day": self.total_tokens_per_day,
        }

    @classmethod
    def from_dict(cls, raw: Optional[Dict[str, Any]]) -> "TokenQuota":
        raw = raw or {}
        return cls(
            calls_per_minute=_nullable_int(raw.get("calls_per_minute")),
            calls_per_day=_nullable_int(raw.get("calls_per_day")),
            total_tokens_per_day=_nullable_int(raw.get("total_tokens_per_day")),
        )

    def is_empty(self) -> bool:
        return (
            self.calls_per_minute is None
            and self.calls_per_day is None
            and self.total_tokens_per_day is None
        )


@dataclass
class TokenEntry:
    id: str
    name: str
    token: str                  # 明文
    created_at: str
    last_used_at: Optional[str] = None
    disabled: bool = False
    allow_models: List[str] = field(default_factory=list)
    deny_models: List[str] = field(default_factory=list)
    allow_ips: List[str] = field(default_factory=list)
    """IP 白名单：每条为 IPv4/IPv6 单 IP 或 CIDR（如 ``192.168.1.0/24``）。
    空 list = 不限制（兼容老 token）。校验在 ``_require_token`` 里做。"""
    quota: TokenQuota = field(default_factory=TokenQuota)

    @property
    def preview(self) -> str:
        """打码版 token——UI 默认显示。

        **保长 mask（前缀可见型）**：保留前 8 字符做识别锚点（刚好能看到
        ``lry_gw_`` 前缀 + 随机部分首字符），后面全部替换为等长 `*`，
        所以 ``len(preview) == len(token)``。

        ──── 设计取舍 ────
        - **保长**：让 preview 与明文字符数完全相等，配 monospace 字体后两态渲染
          宽度天然一致，前端无需任何 width hack。
        - **只露前缀**：不露尾部 4 字符（v1 设计）——尾部侧信信息量与前缀重复，
          反而为薄弱侧信信息；UI 上露完整 ``lry_gw_`` + 1 随机字符足够识别。

        ──── 算法 ────
        - len ≤ 8 → 全 `*`（太短不露任何字符）
        - len > 8 → ``[:8] + '*'*(n-8)``
        """
        t = self.token
        n = len(t)
        if n <= 8:
            return "*" * n
        return f"{t[:8]}{'*' * (n - 8)}"

    def to_dict(self) -> Dict[str, Any]:
        """完整 dict——含明文。管理端点（loopback only）和持久化均用此。"""
        return {
            "id": self.id,
            "name": self.name,
            "token": self.token,
            "preview": self.preview,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "disabled": self.disabled,
            "allow_models": list(self.allow_models),
            "deny_models": list(self.deny_models),
            "allow_ips": list(self.allow_ips),
            "quota": self.quota.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "TokenEntry":
        return cls(
            id=str(raw["id"]),
            name=str(raw.get("name") or raw["id"]),
            token=str(raw["token"]),
            created_at=str(raw.get("created_at") or _now_iso()),
            last_used_at=raw.get("last_used_at"),
            disabled=bool(raw.get("disabled") or False),
            allow_models=list(raw.get("allow_models") or []),
            deny_models=list(raw.get("deny_models") or []),
            allow_ips=list(raw.get("allow_ips") or []),
            quota=TokenQuota.from_dict(raw.get("quota")),
        )


@dataclass
class ResolvedToken:
    """中间件校验通过后传给路由层的"已识别凭证"。"""
    id: str             # "tok_xxx" 或 "(no_auth)"
    name: str
    is_anonymous: bool
    entry: Optional[TokenEntry] = None  # anonymous 时为 None


class QuotaExceeded(Exception):
    """超过配额时抛出。携带 reason + retry_after_secs。"""

    def __init__(self, reason: str, limit: int, current: int, retry_after_secs: int):
        super().__init__(f"quota exceeded: {reason}")
        self.reason = reason
        self.limit = limit
        self.current = current
        self.retry_after_secs = max(1, int(retry_after_secs))


class ModelDenied(Exception):
    """token 的 allow/deny 列表禁止访问该 model。"""

    def __init__(self, token_id: str, model: str):
        super().__init__(f"token '{token_id}' is not allowed to access model '{model}'")
        self.token_id = token_id
        self.model = model


class IpNotAllowed(Exception):
    """调用方 IP 不在该 token 的 allow_ips 白名单内。"""

    def __init__(self, token_id: str, client_ip: str):
        super().__init__(f"token '{token_id}' rejects client ip '{client_ip}'")
        self.token_id = token_id
        self.client_ip = client_ip


class InvalidIpEntry(ValueError):
    """allow_ips 列表里某条不是合法的 IP / CIDR。create / update 时拋。"""

    def __init__(self, bad: str):
        super().__init__(f"invalid IP / CIDR entry: {bad!r}")
        self.bad = bad


# ─────────────────────── 持久化 ───────────────────────

_io_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _nullable_int(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        iv = int(v)
        return iv if iv > 0 else None
    except (TypeError, ValueError):
        return None


def _new_token_id() -> str:
    return "tok_" + secrets.token_hex(4)


def _new_plain_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def _load_raw() -> Dict[str, Any]:
    p = paths.gateway_tokens_path()
    if not p.exists():
        return {"version": STORAGE_VERSION, "tokens": []}
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Gateway/tokens] tokens.json 损坏，按空集处理：%s", exc)
        return {"version": STORAGE_VERSION, "tokens": []}


def _save_raw(data: Dict[str, Any]) -> None:
    """原子写 + POSIX 0600 权限。"""
    p = paths.gateway_tokens_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    # POSIX 系统加 0600；Windows 调 chmod 无害（仅设 readonly bit）
    try:
        if os.name == "posix":
            os.chmod(p, 0o600)
    except OSError as exc:  # noqa: BLE001
        logger.warning("[Gateway/tokens] chmod 0600 失败（忽略）：%s", exc)


def list_tokens() -> List[TokenEntry]:
    with _io_lock:
        raw = _load_raw()
    out: List[TokenEntry] = []
    for item in raw.get("tokens") or []:
        try:
            out.append(TokenEntry.from_dict(item))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Gateway/tokens] 跳过损坏条目：%s", exc)
    return out


def get_token(token_id: str) -> Optional[TokenEntry]:
    for e in list_tokens():
        if e.id == token_id:
            return e
    return None


def first_active_token() -> Optional[TokenEntry]:
    """installer / launcher 用：返回首条未禁用的令牌。"""
    for e in list_tokens():
        if not e.disabled:
            return e
    return None


def bootstrap_if_empty() -> Optional[TokenEntry]:
    """文件不存在时自动建一条"默认令牌"。

    用户清空 tokens 数组后不再 bootstrap（文件已存在 → 视为用户主动状态）。
    返回新建的 entry；若不需要建则返 None。
    """
    p = paths.gateway_tokens_path()
    if p.exists():
        return None
    entry = TokenEntry(
        id=_new_token_id(),
        name=DEFAULT_TOKEN_NAME,
        token=_new_plain_token(),
        created_at=_now_iso(),
    )
    with _io_lock:
        # 双重检查——并发场景下另一个线程可能已经写入
        if p.exists():
            return None
        _save_raw({"version": STORAGE_VERSION, "tokens": [entry.to_dict()]})
    return entry


def create_token(
    name: str,
    *,
    allow_models: Optional[List[str]] = None,
    deny_models: Optional[List[str]] = None,
    allow_ips: Optional[List[str]] = None,
    quota: Optional[Dict[str, Any]] = None,
) -> TokenEntry:
    """新建令牌；明文随时可读，到此直接返回完整 entry。

    ``allow_ips`` 非空时每条都会用 :func:`normalize_ip_entries` 校验 + 归一化；
    任一不合法将抛 :class:`InvalidIpEntry`，整个创建动作失败（fail-fast）。
    """
    name = (name or "").strip() or "未命名"
    entry = TokenEntry(
        id=_new_token_id(),
        name=name,
        token=_new_plain_token(),
        created_at=_now_iso(),
        disabled=False,
        allow_models=list(allow_models or []),
        deny_models=list(deny_models or []),
        allow_ips=normalize_ip_entries(allow_ips or []),
        quota=TokenQuota.from_dict(quota),
    )
    with _io_lock:
        raw = _load_raw()
        tokens = raw.get("tokens") or []
        while any(t.get("id") == entry.id for t in tokens):
            entry.id = _new_token_id()
        tokens.append(entry.to_dict())
        raw["tokens"] = tokens
        raw["version"] = STORAGE_VERSION
        _save_raw(raw)
    return entry


def update_token(
    token_id: str,
    *,
    name: Optional[str] = None,
    disabled: Optional[bool] = None,
    allow_models: Optional[List[str]] = None,
    deny_models: Optional[List[str]] = None,
    allow_ips: Optional[List[str]] = None,
    quota: Optional[Dict[str, Any]] = None,
) -> Optional[TokenEntry]:
    """传 None 表示该字段不动；传空 list 表示清空。

    ``allow_ips`` 在落盘前会跑 :func:`normalize_ip_entries`；任一不合法抛
    :class:`InvalidIpEntry`，本次更新整体不生效。
    """
    # 提前校验 allow_ips（避免拿着锁失败）
    if allow_ips is not None:
        allow_ips_norm = normalize_ip_entries(allow_ips)
    else:
        allow_ips_norm = None
    with _io_lock:
        raw = _load_raw()
        tokens = raw.get("tokens") or []
        idx = next((i for i, t in enumerate(tokens) if t.get("id") == token_id), -1)
        if idx < 0:
            return None
        item = tokens[idx]
        if name is not None:
            n = name.strip()
            if n:
                item["name"] = n
        if disabled is not None:
            item["disabled"] = bool(disabled)
        if allow_models is not None:
            item["allow_models"] = list(allow_models)
        if deny_models is not None:
            item["deny_models"] = list(deny_models)
        if allow_ips_norm is not None:
            item["allow_ips"] = allow_ips_norm
        if quota is not None:
            item["quota"] = TokenQuota.from_dict(quota).to_dict()
        tokens[idx] = item
        raw["tokens"] = tokens
        _save_raw(raw)
        return TokenEntry.from_dict(item)


def delete_token(token_id: str) -> bool:
    with _io_lock:
        raw = _load_raw()
        tokens = raw.get("tokens") or []
        new_list = [t for t in tokens if t.get("id") != token_id]
        if len(new_list) == len(tokens):
            return False
        raw["tokens"] = new_list
        _save_raw(raw)
    _counters.drop(token_id)
    return True


def rotate_token(token_id: str) -> Optional[TokenEntry]:
    """重置令牌的明文（保留 id / 名字 / 配额 / 限制）。"""
    new_plain = _new_plain_token()
    with _io_lock:
        raw = _load_raw()
        tokens = raw.get("tokens") or []
        idx = next((i for i, t in enumerate(tokens) if t.get("id") == token_id), -1)
        if idx < 0:
            return None
        item = tokens[idx]
        item["token"] = new_plain
        item["last_used_at"] = None
        item.pop("preview", None)
        tokens[idx] = item
        _save_raw(raw)
        entry = TokenEntry.from_dict(item)
    _counters.drop(token_id)
    return entry


def _touch_last_used(token_id: str) -> None:
    """标记 last_used_at；写盘失败静默。"""
    try:
        with _io_lock:
            raw = _load_raw()
            tokens = raw.get("tokens") or []
            for t in tokens:
                if t.get("id") == token_id:
                    t["last_used_at"] = _now_iso()
                    _save_raw(raw)
                    return
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Gateway/tokens] touch last_used_at 失败（忽略）：%s", exc)


# ─────────────────────── 鉴权 ───────────────────────


def resolve_presented(presented: Optional[str]) -> Optional[ResolvedToken]:
    """识别 presented token 对应哪条记录；不存在或被禁用返 None。"""
    if not presented:
        return None
    for entry in list_tokens():
        if entry.disabled:
            continue
        if secrets.compare_digest(entry.token, presented):
            _touch_last_used(entry.id)
            return ResolvedToken(id=entry.id, name=entry.name, is_anonymous=False, entry=entry)
    return None


def anonymous() -> ResolvedToken:
    """require_token=false 时使用的占位身份。"""
    return ResolvedToken(id="(no_auth)", name="(no_auth)", is_anonymous=True, entry=None)


# ─────────────────────── allow/deny 准入 ───────────────────────


def _model_matches(pattern: str, requested: str) -> bool:
    """完全相同 / 'provider:*' 前缀 / '*' 全通配。"""
    if pattern == requested:
        return True
    if pattern.endswith(":*"):
        return requested.startswith(pattern[:-1])
    if pattern == "*":
        return True
    return False


def check_model_allowed(resolved: ResolvedToken, model: Optional[str]) -> None:
    """不允许时抛 ModelDenied。anonymous 不受限。"""
    if resolved.entry is None or not model:
        return
    e = resolved.entry
    if e.deny_models and any(_model_matches(p, model) for p in e.deny_models):
        raise ModelDenied(e.id, model)
    if e.allow_models and not any(_model_matches(p, model) for p in e.allow_models):
        raise ModelDenied(e.id, model)


# ─────────────────────── IP 白名单 ───────────────────────


def normalize_ip_entries(items: List[str]) -> List[str]:
    """清洗用户输入的 allow_ips 列表。

    - 去 strip / 去空行 / 去重
    - 单 IP 用 :class:`ipaddress.ip_address` 校验，归一化（``"127.0.0.001"`` → ``"127.0.0.1"``）
    - CIDR 用 :class:`ipaddress.ip_network` 校验（``strict=False`` 容忍主机位非零）
    - 任一不合法抛 :class:`InvalidIpEntry`
    """
    out: List[str] = []
    seen: set[str] = set()
    for raw in items or []:
        s = (raw or "").strip()
        if not s:
            continue
        norm = _validate_one(s)
        if norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return out


def _validate_one(s: str) -> str:
    try:
        if "/" in s:
            return str(ipaddress.ip_network(s, strict=False))
        return str(ipaddress.ip_address(s))
    except ValueError as exc:  # noqa: BLE001
        raise InvalidIpEntry(s) from exc


def _ip_in_allowlist(client_ip: str, allow_ips: List[str]) -> bool:
    """client_ip 是否落在 allow_ips 任一条目内。

    allow_ips 为空表示不限制 → True。
    client_ip 解析失败 → False（保险拒绝）。
    """
    if not allow_ips:
        return True
    try:
        client = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for entry in allow_ips:
        try:
            if "/" in entry:
                if client in ipaddress.ip_network(entry, strict=False):
                    return True
            else:
                if client == ipaddress.ip_address(entry):
                    return True
        except ValueError:
            # 落盘的脏数据：跳过这条，继续看下一条
            continue
    return False


def check_client_ip(resolved: ResolvedToken, client_ip: str) -> None:
    """不允许时抛 IpNotAllowed。anonymous / 空白名单不受限。"""
    if resolved.entry is None:
        return
    if _ip_in_allowlist(client_ip, resolved.entry.allow_ips):
        return
    raise IpNotAllowed(resolved.entry.id, client_ip)


# ─────────────────────── 配额计量（内存滑动窗口） ───────────────────────


@dataclass
class TokenCounters:
    call_timestamps: Deque[float] = field(default_factory=deque)
    token_events: Deque[Tuple[float, int]] = field(default_factory=deque)


class _CountersStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._map: Dict[str, TokenCounters] = {}

    def _get(self, token_id: str) -> TokenCounters:
        c = self._map.get(token_id)
        if c is None:
            c = TokenCounters()
            self._map[token_id] = c
        return c

    def drop(self, token_id: str) -> None:
        with self._lock:
            self._map.pop(token_id, None)

    def reset_all(self) -> None:
        with self._lock:
            self._map.clear()

    def snapshot(self, token_id: str, now: Optional[float] = None) -> Dict[str, int]:
        now = now if now is not None else time.time()
        with self._lock:
            c = self._map.get(token_id)
            if c is None:
                return {"calls_last_minute": 0, "calls_last_day": 0, "tokens_last_day": 0}
            _prune(c, now)
            calls_1m = sum(1 for ts in c.call_timestamps if ts >= now - 60)
            calls_1d = len(c.call_timestamps)
            tokens_1d = sum(v for _, v in c.token_events)
            return {
                "calls_last_minute": calls_1m,
                "calls_last_day": calls_1d,
                "tokens_last_day": tokens_1d,
            }

    def check_and_reserve(self, entry: TokenEntry, now: Optional[float] = None) -> None:
        q = entry.quota
        if q.is_empty():
            with self._lock:
                self._get(entry.id).call_timestamps.append(now if now is not None else time.time())
            return
        now = now if now is not None else time.time()
        with self._lock:
            c = self._get(entry.id)
            _prune(c, now)
            calls_1m = sum(1 for ts in c.call_timestamps if ts >= now - 60)
            calls_1d = len(c.call_timestamps)
            tokens_1d = sum(v for _, v in c.token_events)
            if q.calls_per_minute is not None and calls_1m >= q.calls_per_minute:
                oldest_1m = min((ts for ts in c.call_timestamps if ts >= now - 60), default=now)
                retry = max(1, int(oldest_1m + 60 - now))
                raise QuotaExceeded("calls_per_minute", q.calls_per_minute, calls_1m, retry)
            if q.calls_per_day is not None and calls_1d >= q.calls_per_day:
                oldest_1d = c.call_timestamps[0] if c.call_timestamps else now
                retry = max(60, int(oldest_1d + 86400 - now))
                raise QuotaExceeded("calls_per_day", q.calls_per_day, calls_1d, retry)
            if q.total_tokens_per_day is not None and tokens_1d >= q.total_tokens_per_day:
                oldest_1d = c.token_events[0][0] if c.token_events else now
                retry = max(60, int(oldest_1d + 86400 - now))
                raise QuotaExceeded("total_tokens_per_day", q.total_tokens_per_day, tokens_1d, retry)
            c.call_timestamps.append(now)

    def record_tokens(self, token_id: str, total: int, now: Optional[float] = None) -> None:
        if total <= 0:
            return
        now = now if now is not None else time.time()
        with self._lock:
            self._get(token_id).token_events.append((now, int(total)))


def _prune(c: TokenCounters, now: float) -> None:
    cutoff_1d = now - 86400
    while c.call_timestamps and c.call_timestamps[0] < cutoff_1d:
        c.call_timestamps.popleft()
    while c.token_events and c.token_events[0][0] < cutoff_1d:
        c.token_events.popleft()


_counters = _CountersStore()


def get_counters() -> _CountersStore:
    return _counters


def check_quota(resolved: ResolvedToken) -> None:
    if resolved.entry is None:
        return
    _counters.check_and_reserve(resolved.entry)


def record_response_tokens(resolved: ResolvedToken, total_tokens: int) -> None:
    if resolved.entry is None or total_tokens <= 0:
        return
    _counters.record_tokens(resolved.id, total_tokens)


def runtime_usage(token_id: str) -> Dict[str, int]:
    return _counters.snapshot(token_id)


__all__ = [
    "TOKEN_PREFIX",
    "DEFAULT_TOKEN_NAME",
    "STORAGE_VERSION",
    "TokenQuota",
    "TokenEntry",
    "ResolvedToken",
    "QuotaExceeded",
    "ModelDenied",
    "IpNotAllowed",
    "InvalidIpEntry",
    "normalize_ip_entries",
    "check_client_ip",
    "list_tokens",
    "get_token",
    "first_active_token",
    "bootstrap_if_empty",
    "create_token",
    "update_token",
    "delete_token",
    "rotate_token",
    "resolve_presented",
    "anonymous",
    "check_quota",
    "check_model_allowed",
    "record_response_tokens",
    "runtime_usage",
    "get_counters",
]
