"""guardian_config.json 落盘 + 校验。

落盘到 `data_dir() / "guardian_config.json"`。Guardian.reload_config() 优先读
本文件、回填到 in-memory 单例，没有的字段走硬编码 defaults。

结构：
{
  "version": "1.0",
  "defaults": {
    "private_files": [...],         # list[str] - 隐私文件名（其他角色禁读）
    "public_files":  [...],         # list[str] - 公开文件名（其他角色可读）
    "other_private":     "ALLOW"|"DENY",
    "write_other_agent": "ALLOW"|"DENY",
    "delete":            "ALLOW"|"DENY",
    "execute_command":   "ALLOW"|"DENY",
    "write_backend":     "ALLOW"|"DENY"
  },
  "overrides": {
    "<agent_id>": { 同 defaults 的 5 个开关字段，子集即可 }
  }
}

硬编码默认 = 当前 Guardian 实际行为（不破坏现有），未来 M2 上线 CONFIRM 路径时
delete / execute_command 默认值改 CONFIRM 即可，schema 不破坏（多增枚举值）。

不维护 .secrets——权限规则不含敏感信息（敏感信息是行为本身，不是配置）。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from . import paths

logger = logging.getLogger(__name__)


_RULE_FIELDS = (
    "other_private",
    "write_other_agent",
    "delete",
    "execute_command",
    "write_backend",
)
_LIST_FIELDS = ("private_files", "public_files")
_ACTION_VALUES = ("ALLOW", "DENY")


HARDCODED_DEFAULTS: Dict[str, Any] = {
    "private_files": ["SOUL.md", "REFLECTIONS.md", "meta.json", "memory.json", "emotions.json"],
    "public_files": ["GENOME.md", "SKILLS.md", "avatar.png"],
    "other_private": "DENY",
    "write_other_agent": "DENY",
    "delete": "ALLOW",
    "execute_command": "ALLOW",
    "write_backend": "DENY",
}


def load_guardian_config() -> Dict[str, Any]:
    """读 guardian_config.json。文件不存在 / JSON 损坏 → {}。"""
    path = paths.guardian_config_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        logger.warning("guardian_config.json 损坏 (%s)，按空配置处理", exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    return _sanitize_config(raw)


def save_guardian_config(cfg: Dict[str, Any]) -> None:
    path = paths.guardian_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def validate_guardian_payload(body: Dict[str, Any]) -> Dict[str, Any]:
    """前端 PUT body → 落盘格式。

    - defaults / overrides 字段独立校验（部分更新友好）
    - 未知字段忽略
    - 类型/枚举错抛 ValueError
    - 不校验 overrides 的 agent_id 是否存在（路由层做，需要 AgentConfigManager）
    """
    if not isinstance(body, dict):
        raise ValueError("payload 必须是 object")

    out: Dict[str, Any] = {}

    if "version" in body:
        out["version"] = str(body["version"])

    if "defaults" in body:
        out["defaults"] = _validate_defaults(body["defaults"])

    if "overrides" in body:
        ov = body["overrides"]
        if not isinstance(ov, dict):
            raise ValueError("overrides 必须是 object")
        cleaned_ov: Dict[str, Dict[str, Any]] = {}
        for agent_id, rules in ov.items():
            if not isinstance(agent_id, str) or not agent_id.strip():
                raise ValueError(f"overrides 的 agent_id 必须是非空字符串")
            if not isinstance(rules, dict):
                raise ValueError(f"overrides[{agent_id}] 必须是 object")
            cleaned_rules = _validate_overrides_rules(rules, agent_id)
            if cleaned_rules:
                cleaned_ov[agent_id] = cleaned_rules
        out["overrides"] = cleaned_ov

    return out


def merge_with_defaults(file_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """把落盘 config 与硬编码 defaults 合并，得到 effective defaults。

    file_cfg.defaults 是用户改过的子集，没改的字段从 HARDCODED_DEFAULTS 取。
    返回结构 = HARDCODED_DEFAULTS 形状（所有字段齐全）。
    """
    effective = dict(HARDCODED_DEFAULTS)
    file_defaults = file_cfg.get("defaults") or {}
    for key, value in file_defaults.items():
        if key in HARDCODED_DEFAULTS:
            effective[key] = value
    return effective


def resolve_for_agent(file_cfg: Dict[str, Any], agent_id: str) -> Dict[str, Any]:
    """计算某个 agent 的最终生效规则：HARDCODED_DEFAULTS ← file.defaults ← file.overrides[agent]"""
    effective = merge_with_defaults(file_cfg)
    overrides = (file_cfg.get("overrides") or {}).get(agent_id, {})
    for key in _RULE_FIELDS:
        if key in overrides:
            effective[key] = overrides[key]
    return effective


# ===== internal =====

def _sanitize_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    """读盘后做容错清理：字段类型不对就丢弃，不抛错（容错优先）。"""
    out: Dict[str, Any] = {}
    if isinstance(raw.get("version"), str):
        out["version"] = raw["version"]
    defaults = raw.get("defaults")
    if isinstance(defaults, dict):
        out["defaults"] = _sanitize_defaults(defaults)
    overrides = raw.get("overrides")
    if isinstance(overrides, dict):
        cleaned_ov: Dict[str, Dict[str, Any]] = {}
        for agent_id, rules in overrides.items():
            if not isinstance(agent_id, str) or not isinstance(rules, dict):
                continue
            cleaned = _sanitize_rules(rules)
            if cleaned:
                cleaned_ov[agent_id] = cleaned
        out["overrides"] = cleaned_ov
    return out


def _sanitize_defaults(defaults: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in _LIST_FIELDS:
        val = defaults.get(key)
        if isinstance(val, list) and all(isinstance(x, str) for x in val):
            out[key] = list(val)
    out.update(_sanitize_rules(defaults))
    return out


def _sanitize_rules(rules: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in _RULE_FIELDS:
        val = rules.get(key)
        if isinstance(val, str) and val in _ACTION_VALUES:
            out[key] = val
    return out


def _validate_defaults(defaults: Any) -> Dict[str, Any]:
    if not isinstance(defaults, dict):
        raise ValueError("defaults 必须是 object")
    out: Dict[str, Any] = {}
    for key in _LIST_FIELDS:
        if key not in defaults:
            continue
        val = defaults[key]
        if not isinstance(val, list):
            raise ValueError(f"defaults.{key} 必须是数组")
        for x in val:
            if not isinstance(x, str):
                raise ValueError(f"defaults.{key} 元素必须是字符串")
        out[key] = list(val)
    for key in _RULE_FIELDS:
        if key not in defaults:
            continue
        val = defaults[key]
        if val not in _ACTION_VALUES:
            raise ValueError(
                f"defaults.{key} 必须是 'ALLOW' 或 'DENY'，收到 {val!r}"
            )
        out[key] = val
    return out


def _validate_overrides_rules(rules: Dict[str, Any], agent_id: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in _RULE_FIELDS:
        if key not in rules:
            continue
        val = rules[key]
        if val not in _ACTION_VALUES:
            raise ValueError(
                f"overrides[{agent_id}].{key} 必须是 'ALLOW' 或 'DENY'，收到 {val!r}"
            )
        out[key] = val
    return out
