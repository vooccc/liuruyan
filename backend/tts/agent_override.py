"""角色级 TTS 覆盖配置。

设计动机
========
全局 ``tts.json`` 是"默认音色"——所有角色共用。但用户希望**每个角色一个音色**：
柳如烟用 A 音色，寒清雪用 B 音色。这就需要"角色级覆盖全局"机制。

落地方式
========
不动全局 ``tts.json`` schema，**角色级配置存到 agent meta.json 的
``tts_override`` 字段**——agent 自己的目录里，跟角色绑定，撤除 TTS 时一删就净。

字段策略
========
**只允许覆盖音色相关字段**（model / voice / refer_wav_path 等），**不允许覆盖**：

- ``enabled`` / ``mode``：朗读策略是全局的（用户对"什么时候念"的偏好）
- ``api_key`` / ``base_url``：兼容字段，不暴露给角色级
- ``timeout_sec``：网络参数，全局即可

合并规则
========
``merge_into_config(global_cfg, override) -> TTSConfig``：把 override 里**非 None
非空字符串**的字段覆盖到 global_cfg 上，返回新 TTSConfig。空字符串 = 用户清空 =
回退全局，跟 None 同语义（不覆盖）。

零污染
======
- 数据存在 agent meta.json 的 ``tts_override`` 字段下，跟其他角色字段平级
- 撤除 TTS 时：所有 agent meta.json 里的 ``tts_override`` 字段会成为孤儿，**但不影响**
  任何核心逻辑（agent_config 用 free-form dict 读 meta，多余字段忽略即可）
- 不引用任何核心模块；不被 agent.py / chat 流主路径引用（M5 暂存数据，M2 朗读时合并）

设计文档：``_docs/handover_tts_design_2026-05-29.md`` §M5
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from pydantic import BaseModel

from .config import ProviderType, ResponseFormat, TTSConfig

logger = logging.getLogger(__name__)


# ── schema ──────────────────────────────────────────────────────


class TTSAgentOverride(BaseModel):
    """角色级 TTS 覆盖。所有字段可选——只覆盖提供的字段。

    跟 TTSConfigUpdate 的差异：
    - 不含 enabled / mode（朗读策略全局统一）
    - 不含 api_key / base_url（兼容字段，不暴露）
    - 不含 timeout_sec（网络参数，全局即可）
    """

    provider_type: Optional[ProviderType] = None
    """允许角色级切协议——比如某些角色用本地 GPT-SoVITS，某些用云端 OpenAI。"""

    # ── OpenAI 兼容字段 ────────────────────────────────────────
    provider_id: Optional[str] = None
    model: Optional[str] = None
    voice: Optional[str] = None
    speed: Optional[float] = None
    response_format: Optional[ResponseFormat] = None

    # ── GPT-SoVITS 字段 ────────────────────────────────────────
    gptsovits_base_url: Optional[str] = None
    """通常不覆盖（本地服务地址全局即可），但允许跨主机角色场景。"""
    gptsovits_refer_wav_path: Optional[str] = None
    gptsovits_prompt_text: Optional[str] = None
    gptsovits_text_lang: Optional[str] = None


# ── 读写 ─────────────────────────────────────────────────────────


def _meta_file_path(agent_id: str):
    """agent meta.json 路径。

    跟 ``backend.agent_config.AgentConfig.meta_file`` 对齐，但不引用 AgentConfig
    保持零污染——TTS 模块独立可拆。
    """
    from .. import paths

    return paths.data_dir() / "agents" / agent_id / "meta.json"


def _load_meta_dict(agent_id: str) -> Dict[str, Any]:
    p = _meta_file_path(agent_id)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8-sig")) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("[TTS-OVERRIDE] failed to read agent meta %s: %s", agent_id, e)
        return {}


def _save_meta_dict(agent_id: str, meta: Dict[str, Any]) -> None:
    p = _meta_file_path(agent_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_agent_override(agent_id: str) -> Optional[TTSAgentOverride]:
    """读取角色级 TTS override。无字段或解析失败 → None。"""
    if not agent_id:
        return None
    meta = _load_meta_dict(agent_id)
    raw = meta.get("tts_override")
    if not isinstance(raw, dict) or not raw:
        return None
    try:
        return TTSAgentOverride.model_validate(raw)
    except Exception as e:  # noqa: BLE001
        logger.warning("[TTS-OVERRIDE] invalid override for %s: %s", agent_id, e)
        return None


def save_agent_override(agent_id: str, override: TTSAgentOverride) -> TTSAgentOverride:
    """保存角色级 override 到 agent meta.json 的 tts_override 字段。

    - 全字段都是 None 时（用户清空）会写一个空 dict（保留 key 让前端能判别"已配置"）；
      要彻底删用 :func:`delete_agent_override`。
    - 写入时只保留**非 None**的字段，避免 meta.json 里堆一堆 null。
    """
    meta = _load_meta_dict(agent_id)
    raw = {k: v for k, v in override.model_dump().items() if v is not None}
    meta["tts_override"] = raw
    _save_meta_dict(agent_id, meta)
    logger.debug("[TTS-OVERRIDE] saved for %s: %s", agent_id, sorted(raw.keys()))
    return override


def delete_agent_override(agent_id: str) -> bool:
    """删除角色级 override。返 True 表示有过 override 被删除，False 表示原本就没有。"""
    meta = _load_meta_dict(agent_id)
    if "tts_override" not in meta:
        return False
    meta.pop("tts_override", None)
    _save_meta_dict(agent_id, meta)
    logger.debug("[TTS-OVERRIDE] cleared for %s", agent_id)
    return True


def update_agent_override(agent_id: str, patch: Dict[str, Any]) -> TTSAgentOverride:
    """partial-update：合并 patch 进当前 override 后写回，返新 override。"""
    current_dict: Dict[str, Any] = {}
    current = load_agent_override(agent_id)
    if current:
        current_dict = current.model_dump(exclude_none=True)
    # patch 里 None 表示"不动"；空字符串表示"清空该字段（回退全局）"
    for k, v in patch.items():
        if v is None:
            continue
        if v == "":
            current_dict.pop(k, None)
        else:
            current_dict[k] = v
    new_override = TTSAgentOverride.model_validate(current_dict)
    return save_agent_override(agent_id, new_override)


# ── 合并到 TTSConfig ─────────────────────────────────────────────


def _is_relative_asset_path(path: str) -> bool:
    """判断 ``path`` 是否是"角色目录内的资产相对路径"。

    支持的相对写法：
    - ``./tts/reference.mp3`` / ``.\\tts\\reference.mp3``：显式相对路径
    - ``tts/reference.mp3``：隐式相对（不含驱动器号、不以 ``/`` 或 ``\\`` 开头）

    返回 False 表示是绝对路径（``D:\\...`` / ``/usr/...``）或网络 URL，
    应原样保留（用户故意指外部文件）。
    """
    if not path:
        return False
    # Windows 绝对路径：``C:\...`` / ``D:\...``
    if len(path) >= 2 and path[1] == ":":
        return False
    # POSIX 绝对路径
    if path.startswith("/") or path.startswith("\\"):
        return False
    # 网络 URL（罕见但允许）
    if "://" in path:
        return False
    return True


def expand_refer_path(path: str, agent_id: str) -> str:
    """把"角色目录相对路径"展开成绝对路径；绝对路径原样返回。

    用于 GPT-SoVITS provider 的 ``gptsovits_refer_wav_path`` 字段。

    例：
        expand_refer_path("./tts/reference.mp3", "alice")
            -> "<data_dir>/agents/alice/tts/reference.mp3"
        expand_refer_path("D:/audio/x.mp3", "alice")
            -> "D:/audio/x.mp3"   # 绝对路径不动

    注意：本函数不检查文件是否存在——存在性检查由 provider 层做（``os.path.exists``）。
    """
    if not _is_relative_asset_path(path):
        return path
    from .. import paths

    # 去掉前缀 "./" / ".\\" 让 PurePath 不把它当 cwd-relative
    cleaned = path.lstrip("./\\")
    abs_path = paths.data_dir() / "agents" / agent_id / cleaned
    return str(abs_path)


def merge_into_config(
    base: TTSConfig,
    override: Optional[TTSAgentOverride],
) -> TTSConfig:
    """把 override 合并进 base，返回新 TTSConfig。

    合并规则：
    - override 字段为 None：保持 base 的值（不覆盖）
    - override 字段为空字符串：保持 base 的值（视为"未填"——用户在 UI 留空 = 不覆盖）
    - 其他：用 override 的值替换 base

    base 不被修改（pydantic ``model_copy(update=...)`` 是 immutable 复制）。
    """
    if override is None:
        return base
    overrides_to_apply: Dict[str, Any] = {}
    for k, v in override.model_dump().items():
        if v is None:
            continue
        if isinstance(v, str) and v == "":
            continue
        overrides_to_apply[k] = v
    if not overrides_to_apply:
        return base
    return base.model_copy(update=overrides_to_apply)


def resolve_effective_config(
    base: TTSConfig,
    agent_id: Optional[str],
) -> TTSConfig:
    """便捷封装：根据 agent_id 加载 override 并合并到 base，返回最终生效配置。

    agent_id 为空 / 没 override / override 解析失败 → 返回 base 不变。

    **路径展开**：合并完后，如果 ``gptsovits_refer_wav_path`` 是相对路径
    （如 ``./tts/reference.mp3``），自动展开成 ``agents/<agent_id>/tts/reference.mp3``
    的绝对路径。这让 meta.json 里的相对路径可以跟随角色目录走（模板复制时无需改路径）。
    """
    if not agent_id:
        return base
    override = load_agent_override(agent_id)
    merged = merge_into_config(base, override)
    # 路径展开（角色相对路径 → 绝对路径）
    if merged.gptsovits_refer_wav_path and _is_relative_asset_path(
        merged.gptsovits_refer_wav_path
    ):
        abs_path = expand_refer_path(merged.gptsovits_refer_wav_path, agent_id)
        merged = merged.model_copy(update={"gptsovits_refer_wav_path": abs_path})
    return merged
