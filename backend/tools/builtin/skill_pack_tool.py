"""Skill 系统工具：load_skill + Pack 管理（install / update / uninstall / list / status）。

让 LLM 自己用 skill（按需加载完整 SKILL.md），并管理柳如烟的插件包
（兼容 Anthropic Claude Code Plugin 协议）。

实施要点：
- 所有耗时操作（git clone / pip install）run 在 asyncio 默认 executor，不阻塞事件循环
- 错误用 SkillPackError；handler 把异常归一成 {"error": str} JSON 字符串
- 装/升/卸成功后自动调 `skill_loader.reload()`，新 skill 立即可用
- 不暴露任意路径写入：所有路径都经过 pack_name 合法性校验
- load_skill 实现 Claude Code 渐进披露契约的「第 2 步：激活」——
  system prompt 里只放索引（name+description），LLM 命中触发词时调本工具拉全文

参考：`backend/skill_pack_manager.py`、`backend/skill_loader.py`、
       `_docs/skill_marketplace_research.md`
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from backend.skill_pack_manager import (
    SkillPackError,
    install_pack,
    list_packs,
    pack_status,
    uninstall_pack,
    update_pack,
)

logger = logging.getLogger(__name__)


def _to_json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _reload_skills_after_change() -> None:
    """装/升/卸后重新加载 SkillLoader，让新 skill 立刻可见。"""
    try:
        # 延迟 import 避免循环
        from backend.skill_loader import get_skill_loader
        get_skill_loader().reload()
    except Exception:
        # reload 失败不影响安装结果，但要记录
        logger.exception("reload_skills_after_change failed")


# ────────────────────────────────────────────────────────────────────────────
# install_skill_pack
# ────────────────────────────────────────────────────────────────────────────

async def _handle_install_skill_pack(**kwargs) -> str:
    source = kwargs.get("source")
    pack_name = kwargs.get("pack_name")
    create_venv = kwargs.get("create_venv", True)
    install_deps = kwargs.get("install_deps", True)
    overwrite = kwargs.get("overwrite", False)

    if not source:
        return _to_json({"error": "source 必填（github 简写如 'owner/repo' 或完整 URL）"})

    try:
        result = await asyncio.to_thread(
            install_pack,
            source=source,
            pack_name=pack_name if pack_name else None,
            create_venv=bool(create_venv),
            install_deps=bool(install_deps),
            overwrite=bool(overwrite),
        )
        _reload_skills_after_change()
        return _to_json(result)
    except SkillPackError as exc:
        return _to_json({"error": str(exc), "kind": "SkillPackError"})
    except Exception as exc:
        logger.exception("install_skill_pack unexpected failure")
        return _to_json({"error": f"unexpected: {exc}"})


# ────────────────────────────────────────────────────────────────────────────
# update_skill_pack
# ────────────────────────────────────────────────────────────────────────────

async def _handle_update_skill_pack(**kwargs) -> str:
    pack_name = kwargs.get("pack_name")
    install_deps = kwargs.get("install_deps", True)

    if not pack_name:
        return _to_json({"error": "pack_name 必填"})

    try:
        result = await asyncio.to_thread(
            update_pack,
            pack_name=pack_name,
            install_deps=bool(install_deps),
        )
        _reload_skills_after_change()
        return _to_json(result)
    except SkillPackError as exc:
        return _to_json({"error": str(exc), "kind": "SkillPackError"})
    except Exception as exc:
        logger.exception("update_skill_pack unexpected failure")
        return _to_json({"error": f"unexpected: {exc}"})


# ────────────────────────────────────────────────────────────────────────────
# uninstall_skill_pack
# ────────────────────────────────────────────────────────────────────────────

async def _handle_uninstall_skill_pack(**kwargs) -> str:
    pack_name = kwargs.get("pack_name")
    if not pack_name:
        return _to_json({"error": "pack_name 必填"})

    try:
        result = await asyncio.to_thread(uninstall_pack, pack_name=pack_name)
        _reload_skills_after_change()
        return _to_json(result)
    except SkillPackError as exc:
        return _to_json({"error": str(exc), "kind": "SkillPackError"})
    except Exception as exc:
        logger.exception("uninstall_skill_pack unexpected failure")
        return _to_json({"error": f"unexpected: {exc}"})


# ────────────────────────────────────────────────────────────────────────────
# list_skill_packs
# ────────────────────────────────────────────────────────────────────────────

def _handle_list_skill_packs(**kwargs) -> str:
    try:
        packs = list_packs()
        return _to_json({"packs": packs, "total": len(packs)})
    except Exception as exc:
        logger.exception("list_skill_packs unexpected failure")
        return _to_json({"error": f"unexpected: {exc}"})


# ────────────────────────────────────────────────────────────────────────────
# skill_pack_status
# ────────────────────────────────────────────────────────────────────────────

def _handle_skill_pack_status(**kwargs) -> str:
    pack_name = kwargs.get("pack_name")
    if not pack_name:
        return _to_json({"error": "pack_name 必填"})
    try:
        return _to_json(pack_status(pack_name=pack_name))
    except SkillPackError as exc:
        return _to_json({"error": str(exc), "kind": "SkillPackError"})
    except Exception as exc:
        logger.exception("skill_pack_status unexpected failure")
        return _to_json({"error": f"unexpected: {exc}"})


# ────────────────────────────────────────────────────────────────────────────
# load_skill —— Claude Code 渐进披露契约的「激活」入口
# ────────────────────────────────────────────────────────────────────────────

def _handle_load_skill(**kwargs) -> str:
    """按 skill name 拉取完整 SKILL.md 内容（含可选的角色本地 mastery overlay）。

    历史 bug：之前 SkillLoader 在 system prompt 里反复教 LLM「调用 load_skill
    工具加载完整内容」，但这个工具从未注册到 registry。LLM 看到索引、
    想触发 SOP 也调不到入口，只能用 read_file/multi_edit 自由发挥——
    导致整个 webnovel-writer 等渐进披露型 skill 系统形同虚设。
    本 handler 实现该契约的第 2 步「激活」：返回 SKILL.md 全文给 LLM。

    参数：
    - name (str, required): skill 全名（同索引展示），如
      'webnovel-writer__webnovel-init' 或 'humanizer'。
    - agent_id 由 dispatch 自动从调用上下文注入，用于挂角色本地 mastery overlay。
    """
    name = kwargs.get("name")
    agent_id = kwargs.get("agent_id")
    if not name or not isinstance(name, str):
        return _to_json({"error": "name 必填且必须是字符串"})
    try:
        # 延迟 import 避免循环
        from backend.skill_loader import get_skill_content_for_agent
        content = get_skill_content_for_agent(name.strip(), agent_id=agent_id)
        # 直接返回 markdown 文本（不包 JSON）：LLM 直接消费 SKILL.md 内容
        return content
    except Exception as exc:
        logger.exception("load_skill failed: name=%s agent_id=%s", name, agent_id)
        return _to_json({"error": f"unexpected: {exc}"})


__all__ = [
    "_handle_load_skill",
    "_handle_install_skill_pack",
    "_handle_update_skill_pack",
    "_handle_uninstall_skill_pack",
    "_handle_list_skill_packs",
    "_handle_skill_pack_status",
]
