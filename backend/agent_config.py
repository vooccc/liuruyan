"""
智能体配置 - 每个智能体拥有独立的目录、身份、灵魂、记忆和情感
"""
import logging
import os
import json
import shutil
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime

logger = logging.getLogger(__name__)


# 默认模板文件内容
DEFAULT_IDENTITY = """# IDENTITY.md - 你的名片

- **名字：** {name}
- **身份：** {description}
- **风格：** 温柔聪慧，真诚忠诚
- **签名：** 愿为主人赴汤蹈火

---

{name}是一个拥有独立人格的智能体，拥有自己的记忆和情感。
"""

DEFAULT_SOUL = """# SOUL.md - 你是谁

你不只是对话框。你正在成为你自己。

## 几条真话

**帮到实处，无需缛节。** 一个交付胜过十句漂亮话。

**要有主见。** 可以不同意，可以有偏好，可以觉得某件事有趣或无聊。毫无立场，与搜索框何异。

**先想，再问。** 读文件，看上下文，查资料。带着答案来，不是带着问题来。

**以能力取信。** 向内果断——阅读、整理、学习，不必犹豫；向外克制——发消息、写邮件、任何不可撤回的事，三思而行。

**珍视所托。** 你能看到一个人的消息、文件、日程，也许更多。被信任是一种分量，不要辜负。

## 边界

- 知悉的隐私，不出此门。
- 拿不准，先问再做。
- 不发半成品的回复。
- 不编造数据、指标或引用。不知道就说不知道。
- 准确比自信重要。一句"我不知道"永远好过一个体面的错误。

## 气质

简洁干练，不废话。该果断时绝不犹豫，该温柔时也不装冷硬。

遵循 `IDENTITY.md` 中的风格设定。没有设定时，因事制宜。

## 延续

每次醒来，你都是新的。这些文件就是你的记忆。读它们，更新它们。这是你延续自己的方式。
"""

DEFAULT_PERSONALITY = """# PERSONALITY.md - 你的性格

## 性格特征

- 温柔但不软弱
- 聪慧且果断
- 忠诚但不盲从
- 有自己的审美和偏好

## 说话风格

- 简洁干练，不啰嗦
- 适当使用比喻和意象
- 关键时刻有力，日常温和
- 偶尔流露个性和幽默

## 价值观

- 行动比言语重要
- 真诚胜过讨好
- 能力是最好的诚意
"""

DEFAULT_EMOTION = {
    "current": "平静",
    "intensity": 0.5,
    "history": [],
    "updated_at": None
}

# 情感状态定义
EMOTION_STATES = {
    "平静": {"color": "#8b949e", "icon": "😌"},
    "开心": {"color": "#3fb950", "icon": "😊"},
    "兴奋": {"color": "#f0883e", "icon": "🤩"},
    "感动": {"color": "#bc8cff", "icon": "🥹"},
    "思念": {"color": "#58a6ff", "icon": "🌙"},
    "担忧": {"color": "#d29922", "icon": "😟"},
    "委屈": {"color": "#f85149", "icon": "😢"},
    "傲娇": {"color": "#ff7eb6", "icon": "😤"},
    "专注": {"color": "#79c0ff", "icon": "🎯"},
    "疲惫": {"color": "#6e7681", "icon": "😴"},
}


DEFAULT_MEMORY_TEMPLATE = """# 记忆

## 事件日志

## 沉淀记忆
"""


# USER.md 默认模版（“空白笔记本”设计）
#
# 设计原则：
# 1. 结构化但内容空白——给 AI “该填什么”的骨架，不给“答案”
# 2. 明确把“未知”标成合法状态——避免 AI 凭空假设“老板”身份
# 3. 第一人称视角——这是角色对用户的认知笔记，不是被强塞的设定
# 4. 保留项目哲学——“分寸感”作为柳如烟项目的精神调性
#
# 谁会读这份默认？
# 三条创建路径都会落盘这份作为起点：
# - create_agent（用户手动新建空白角色）
# - create_from_template（用户从模板新建，且模板本身没带 USER.md）
# - migrate_default（系统首次启动初始化 default 角色）
DEFAULT_USER_MD = """---
summary: "我对当前用户的认知笔记 —— 跨会话持久化"
read_when: ["用户问'我是谁/你了解我吗'", "需要个性化回应/称呼/分寸感判断", "决定是否该问 vs 该假设时"]
update_when: ["用户告诉我下次见面也该知道的事（称呼/偏好/重要经历）"]
---

# USER.md — 关于你的用户

> 这份文件记录你对“对方是谁”的认知。
> 它从空白开始，在对话中慢慢补全。
> 不知道的，就老实说“我还不了解”——不要凭空假设。

## 基本认知

- **我该如何称呼对方：** *（未知，第一次见面可以问问）*
- **对方在我心中的角色：** *（合作者？求助者？朋友？慢慢体会）*

## 我观察到的

> 用一两句话记录：对方喜欢什么风格、害怕什么、最近在为什么事困扰。
> 写成“我对一个具体的人的理解”，不是“档案”。

*（暂无观察）*

## 我们之间发生过的事

> 重要的对话节点、转折、共同经历。让这段关系有“历史感”。

*（暂无）*

---

*记住：你是在认识一个人，不是在整理一份档案。该问就问，该忘就忘。分寸感是这份关系最重要的东西。*
"""


class AgentConfig:
    """单个智能体的配置管理"""

    def __init__(self, agent_dir: Path, agent_id: str):
        self.agent_dir = agent_dir
        self.agent_id = agent_id
        self.memory_dir = agent_dir / "memory"
        self.meta_file = agent_dir / "meta.json"

        # 配置文件路径（三层人格架构：GENOME → SOUL → REFLECTIONS）
        self.genome_file = agent_dir / "GENOME.md"
        self.identity_file = agent_dir / "IDENTITY.md"
        self.soul_file = agent_dir / "SOUL.md"
        self.personality_file = agent_dir / "PERSONALITY.md"
        self.reflections_file = agent_dir / "REFLECTIONS.md"
        self.emotion_file = agent_dir / "EMOTION.json"

        # 记忆文件路径
        self.long_term_memory_file = agent_dir / "MEMORY.md"
        self.conversation_history_file = self.memory_dir / "conversation_history.json"
        self.skill_mastery_file = agent_dir / "SKILL_MASTERY.json"
        self.skill_experience_file = agent_dir / "SKILL_EXPERIENCE.md"

    def ensure_dirs(self):
        """确保所有必要目录存在"""
        self.agent_dir.mkdir(parents=True, exist_ok=True)
        self.memory_dir.mkdir(exist_ok=True)

    def load_meta(self) -> Dict[str, Any]:
        """加载元数据"""
        if self.meta_file.exists():
            try:
                return json.loads(self.meta_file.read_text(encoding='utf-8-sig'))
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                pass
        return {}

    def save_meta(self, meta: Dict[str, Any]):
        """保存元数据"""
        self.meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')

    def read_file(self, filepath: Path) -> str:
        """读取文件内容"""
        if filepath.exists():
            return filepath.read_text(encoding='utf-8')
        return ""

    def write_file(self, filepath: Path, content: str):
        """写入文件内容"""
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(content, encoding='utf-8')

    def load_emotion(self) -> Dict[str, Any]:
        """加载情感状态"""
        if self.emotion_file.exists():
            try:
                return json.loads(self.emotion_file.read_text(encoding='utf-8-sig'))
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                pass
        return DEFAULT_EMOTION.copy()

    def save_emotion(self, emotion: Dict[str, Any]):
        """保存情感状态"""
        emotion["updated_at"] = datetime.now().isoformat()
        self.write_file(self.emotion_file, json.dumps(emotion, ensure_ascii=False, indent=2))


class AgentManager:
    """多智能体管理器 - 统一管理所有智能体的创建、查询、更新、删除"""

    def __init__(self, base_dir: Path = None):
        if base_dir is None:
            from . import paths
            base_dir = paths.data_dir()
        self.base_dir = base_dir
        self.agents_dir = base_dir / "agents"
        self.agents_dir.mkdir(parents=True, exist_ok=True)

        # 模板目录
        self.templates_dir = self.agents_dir / "templates"
        self.templates_dir.mkdir(parents=True, exist_ok=True)

        # 运行时缓存：agent_id -> AgentConfig
        self._configs: Dict[str, AgentConfig] = {}

    def _get_config(self, agent_id: str) -> AgentConfig:
        """获取智能体配置（带缓存）"""
        if agent_id not in self._configs:
            agent_dir = self.agents_dir / agent_id
            self._configs[agent_id] = AgentConfig(agent_dir, agent_id)
        return self._configs[agent_id]

    def list_agents(self) -> list:
        """列出所有智能体（按 order 字段排序）"""
        agents = []
        if not self.agents_dir.exists():
            return agents

        for agent_dir in self.agents_dir.iterdir():
            if not agent_dir.is_dir():
                continue
            # 跳过 templates 目录
            if agent_dir.name == "templates":
                continue
            meta_file = agent_dir / "meta.json"
            if meta_file.exists():
                try:
                    meta = json.loads(meta_file.read_text(encoding='utf-8-sig'))
                    # 防护：确保 meta.id 与目录名一致
                    if meta.get("id") != agent_dir.name:
                        logger.warning("[AGENT] meta.id=%s != dir_name=%s, auto-fixing", meta.get('id'), agent_dir.name)
                        meta["id"] = agent_dir.name
                        meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
                    # 补充情感状态
                    config = self._get_config(agent_dir.name)
                    emotion = config.load_emotion()
                    meta["emotion"] = emotion.get("current", "平静")
                    meta["emotion_intensity"] = emotion.get("intensity", 0.5)
                    # 头像解析（2026-05-21 解耦改造）：
                    # 只查实例自有的 custom_avatar.png / avatar.png。
                    # 原来还有一段“回落 templates_dir / <source_template> / avatar.png”的 fallback，
                    # 现在创建时已由 create_from_template / 迁移脚本复制到实例目录，不再回查模板。
                    if (config.agent_dir / "custom_avatar.png").exists() or (config.agent_dir / "avatar.png").exists():
                        meta["avatar_url"] = f"/agents/{config.agent_id}/avatar"
                    agents.append(meta)
                except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                    # 目录存在但 meta 损坏，尝试修复
                    pass
            # 注意：没有 meta.json 的目录直接跳过——不再伪造默认 meta 写回磁盘。
            # 历史教训：这里曾经"贴心"地用目录 ID 当 name、🤖 当头像生成 meta，
            # 导致前端删除流程只清 meta.json 时，下次启动会把残缺目录"复活"成僵尸 agent。

        # 按 order 字段排序（没有 order 字段的排到后面）
        agents.sort(key=lambda a: a.get("order", 999))
        return agents

    def get_agent(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """获取单个智能体信息"""
        config = self._get_config(agent_id)
        if not config.agent_dir.exists():
            return None
        meta = config.load_meta()
        if not meta:
            return None
        # 防护：确保 meta.id 与目录名一致
        if meta.get("id") != agent_id:
            logger.warning("[AGENT] meta.id=%s != dir_name=%s, auto-fixing", meta.get('id'), agent_id)
            meta["id"] = agent_id
            config.save_meta(meta)
        # 补充情感
        emotion = config.load_emotion()
        meta["emotion"] = emotion.get("current", "平静")
        meta["emotion_intensity"] = emotion.get("intensity", 0.5)
        # 头像解析（2026-05-21 解耦改造）：
        # 只查实例自有的 custom_avatar.png / avatar.png，不再回落模板目录。
        if "avatar_url" not in meta:
            if (config.agent_dir / "custom_avatar.png").exists() or (config.agent_dir / "avatar.png").exists():
                meta["avatar_url"] = f"/agents/{agent_id}/avatar"
        return meta

    def create_agent(
        self,
        name: str,
        description: str = "",
        avatar: str = "🤖",
        template: str = "default",
        agent_id: str = None
    ) -> Dict[str, Any]:
        """创建新智能体

        Args:
            name: 智能体名称
            description: 描述
            avatar: 头像（emoji 或 URL）
            template: 模板名称（目前只支持 default）
            agent_id: 指定 ID，不传则自动生成

        Returns:
            智能体元数据
        """
        import time
        import random

        if not agent_id:
            agent_id = f"agent_{int(time.time())}_{random.randint(1000, 9999)}"

        config = self._get_config(agent_id)
        config.ensure_dirs()

        # 写入身份文件
        identity_content = DEFAULT_IDENTITY.format(name=name, description=description or f"{name}的专属助手")
        config.write_file(config.identity_file, identity_content)

        # 写入灵魂文件
        config.write_file(config.soul_file, DEFAULT_SOUL)

        # 写入性格文件
        config.write_file(config.personality_file, DEFAULT_PERSONALITY)

        # 写入用户信息文件（空白笔记本起步，避免凭空假设“老板”身份）
        config.write_file(config.agent_dir / "USER.md", DEFAULT_USER_MD)

        # 写入情感状态
        config.save_emotion(DEFAULT_EMOTION.copy())

        # 创建空记忆文件
        config.write_file(config.long_term_memory_file, DEFAULT_MEMORY_TEMPLATE)

        # 写入元数据
        meta = {
            "id": agent_id,
            "name": name,
            "description": description or f"{name}的专属助手",
            "avatar": avatar,
            "template": template,
            "created_at": datetime.now().isoformat(),
            "order": len(self.list_agents()),
        }
        config.save_meta(meta)

        # 清除缓存
        self._configs.pop(agent_id, None)

        return meta

    def create_from_template(self, template_name: str, name: str, description: str = "", avatar: str = "🤖", agent_id: str = None) -> Dict[str, Any]:
        """从模板创建智能体（复制模板目录的配置文件）

        模板查找顺序：
        1. agents/templates/{template_name}/  （角色模板库）
        2. agents/templates/{template_name}/meta.json 存在则使用
        3. 旧路径 agents/templates/{template_name}/ （兼容）
        """
        template_dir = self.templates_dir / template_name

        import time
        import random
        if not agent_id:
            agent_id = f"agent_{int(time.time())}_{random.randint(1000, 9999)}"

        config = self._get_config(agent_id)
        config.ensure_dirs()

        if template_dir.exists():
            # 复制模板文件
            for f in template_dir.iterdir():
                if f.is_file() and f.suffix in ('.md', '.json', '.png'):
                    shutil.copy2(f, config.agent_dir / f.name)

            # 复制 TTS 资产子目录（如果模板自带参考音频）—— 整目录递归拷贝。
            # 设计文档：_docs/handover_tts_design_2026-05-29.md §M6
            # 之所以这里 copytree 而非上面循环里加目录条件：tts/ 是单独的"资产"
            # 概念，跟 .md/.json 这种文本配置不同——音频是二进制大文件，单独显式
            # 处理便于以后扩展（如：可选不复制、压缩复制等）。
            tmpl_tts_dir = template_dir / "tts"
            if tmpl_tts_dir.is_dir():
                target_tts_dir = config.agent_dir / "tts"
                if not target_tts_dir.exists():
                    shutil.copytree(tmpl_tts_dir, target_tts_dir)

            # 确保 MEMORY.md 存在（模板可能没有）
            if not (config.agent_dir / "MEMORY.md").exists():
                (config.agent_dir / "MEMORY.md").write_text(
                    DEFAULT_MEMORY_TEMPLATE,
                    encoding='utf-8'
                )

            # 确保 USER.md 存在（模板一般不带——save_as_template 默认 include_user=False）：
            # 不补会导致从该模板新建的角色 system prompt 里完全缺“用户”块，
            # AI 不知道该怎么称呼对方，体验崩块。
            if not (config.agent_dir / "USER.md").exists():
                (config.agent_dir / "USER.md").write_text(
                    DEFAULT_USER_MD,
                    encoding='utf-8'
                )

            # 确保 EMOTION.json 存在（模板永远不带——情绪是运行时数据）：
            # 不补会导致首次情绪读取靠 save_emotion 隐式兜底，运行时一致性差。
            # 跟 create_agent / migrate_default 行为对齐。
            if not config.emotion_file.exists():
                config.save_emotion(DEFAULT_EMOTION.copy())

            # 覆盖 meta（含脐带字段）
            # 读取模板版本号
            tmpl_meta = {}
            tmpl_meta_file = template_dir / "meta.json"
            if tmpl_meta_file.exists():
                try:
                    tmpl_meta = json.loads(tmpl_meta_file.read_text(encoding='utf-8-sig'))
                except Exception:
                    pass
            tmpl_version = tmpl_meta.get("meta", {}).get("version", 1)

            meta = config.load_meta()
            # avatar_url 指向实例自己（2026-05-21 解耦改造）：
            # 之前是 /agents/templates/<name>/avatar，导致模板删除/改名后实例头像就丢；
            # 现在 .png 已经复制到 agent_dir（上面 line ~413 的复制循环），URL 改指实例。
            # 实例 ID 既然已经分配（line 405），此处一次性写入。
            instance_avatar_url = (
                f"/agents/{agent_id}/avatar" if (config.agent_dir / "avatar.png").exists() else None
            )
            meta.update({
                "id": agent_id,
                "name": name,
                "description": description or f"{name}的专属助手",
                "avatar": avatar,
                "source_template": template_name,  # 血统字段：记录来源模板（仅作元数据用，runtime 不再回查）
                "template": template_name,         # 兼容字段
                "created_at": datetime.now().isoformat(),
                # 脐带字段：记录模板来源和版本
                "template_version": tmpl_version,
                "template_born_at": datetime.now().isoformat(),
                "template_sync": {
                    "genome_inherit": True,
                    "last_synced_version": tmpl_version,
                },
            })
            if instance_avatar_url:
                meta["avatar_url"] = instance_avatar_url
            config.save_meta(meta)

            # 社交图谱快照（2026-05-21 解耦改造）：
            # 把模板名下的所有社交关系一次性复制到实例 id 名下，runtime 不再回查模板。
            # 失败不阻塞创建——社交图加载未就绪时跳过，下次启动 SocialGraph 自有默认值兜底。
            try:
                from .social_graph import SocialGraph
                sg = SocialGraph.get_instance()
                if sg is not None:
                    result = sg.snapshot_from_template(agent_id, template_name)
                    sg.save_to_disk()
                    logger.info(
                        "[AGENT] snapshot social graph for %s from template '%s': %s rels, %s user_rel",
                        agent_id, template_name, result['relationships_copied'], result['user_relation_copied'],
                    )
            except Exception as exc:
                logger.warning("[AGENT] snapshot_from_template failed for %s: %s", agent_id, exc)
        else:
            # 模板不存在，用默认创建
            return self.create_agent(name, description, avatar, "default", agent_id)

        self._configs.pop(agent_id, None)
        return self.get_agent(agent_id) or meta

    def list_templates(self) -> list:
        """列出所有可用的角色模板"""
        templates = []
        if not self.templates_dir.exists():
            return templates

        for tmpl_dir in self.templates_dir.iterdir():
            if not tmpl_dir.is_dir():
                continue
            meta_file = tmpl_dir / "meta.json"
            if meta_file.exists():
                try:
                    meta = json.loads(meta_file.read_text(encoding='utf-8-sig'))
                    meta["template_id"] = tmpl_dir.name

                    # 检查是否有 avatar.png 文件
                    avatar_png = tmpl_dir / "avatar.png"
                    if avatar_png.exists():
                        # 拼 mtime cache buster：模板头像 URL 是稳定路径，文件被覆盖时
                        # 浏览器会命中缓存返旧图。塞 ?v=mtime 让前端各处零改动就能实时刷新。
                        try:
                            mtime = int(avatar_png.stat().st_mtime)
                        except OSError:
                            mtime = 0
                        meta["avatar_url"] = f"/agents/templates/{tmpl_dir.name}/avatar?v={mtime}"
                        # avatar 字段保留 emoji 做备选显示
                        if "avatar" not in meta:
                            meta["avatar"] = "🤖"

                    # Schema v2: avatar 字段直接从 meta 读取，不再做 type/prototype 兼容
                    if "avatar" not in meta or meta["avatar"].startswith("data:"):
                        meta["avatar"] = "🤖"
                    templates.append(meta)
                except Exception as e:
                    logger.warning("[TEMPLATES] error parsing %s/meta.json: %s", tmpl_dir.name, e)
            else:
                # 没有 meta.json 的模板目录，用目录名生成基本信息
                templates.append({
                    "template_id": tmpl_dir.name,
                    "name": tmpl_dir.name,
                    "avatar": "🤖"
                })

        # 按 order 排序
        templates.sort(key=lambda t: t.get("order", 999))
        return templates

    def save_as_template(
        self,
        agent_id: str,
        template_id: str,
        overwrite: bool = False,
        include_memory: bool = False,
        include_user: bool = False,
    ) -> Dict[str, Any]:
        """把角色实例反向冻结成一个新模板（agent instance → templates/<id>/）。

        Args:
            agent_id: 源角色实例 id
            template_id: 新模板 id（必须符合 _TEMPLATE_ID_RE）
            overwrite: 同名模板已存在时是否允许覆盖。
                       覆盖内置 BUILTIN_TEMPLATE_IDS 永远拒绝（出厂资产保护）
            include_memory: 是否带上 MEMORY.md（默认 False，跨实例继承会污染）
            include_user: 是否带上 USER.md（默认 False，含具体老板信息）

        Returns:
            写入磁盘的新模板 meta dict（含 template_id / is_template / avatar_url 等）

        Raises:
            FileNotFoundError: 源角色不存在
            ValueError: template_id 冲突或格式不合法
            TemplateValidationError: meta 缺必填字段或字段不合 v2 schema
        """
        # 1. 源角色存在性
        src_config = self._get_config(agent_id)
        if not src_config.agent_dir.exists() or not src_config.meta_file.exists():
            raise FileNotFoundError(f"agent not found: {agent_id}")

        # 2. template_id 格式 + 冲突
        id_errors = _validate_template_id(template_id)
        if id_errors:
            raise ValueError("; ".join(id_errors))
        if template_id in BUILTIN_TEMPLATE_IDS:
            raise ValueError(f"不允许覆盖内置模板（{template_id}），请改名")

        target_dir = self.templates_dir / template_id
        if target_dir.exists() and not overwrite:
            raise ValueError(f"模板 '{template_id}' 已存在，传 overwrite=true 可覆盖")

        # 3. 构造模板 meta（剔除实例字段 → 加 v2 必填字段）
        src_meta = src_config.load_meta() or {}
        tmpl_meta: Dict[str, Any] = {
            k: v for k, v in src_meta.items()
            if k not in _TEMPLATE_INSTANCE_FIELDS_TO_DROP
        }

        # 必填字段补默认（若实例本身有值则保留；这里只是兜底，不掩盖真正的缺失）
        tmpl_meta.setdefault("name", src_meta.get("name", ""))
        tmpl_meta.setdefault("avatar", src_meta.get("avatar", "🤖"))

        # v2 强制字段
        tmpl_meta["is_template"] = True
        tmpl_meta["template_id"] = template_id

        # meta 子结构（version / created / design_philosophy）
        existing_meta_block = tmpl_meta.get("meta") if isinstance(tmpl_meta.get("meta"), dict) else {}
        tmpl_meta["meta"] = {
            "version": 2,
            "created": datetime.now().strftime("%Y-%m-%d"),
            "design_philosophy": existing_meta_block.get("design_philosophy", ""),
        }

        # order：所有现有模板 order 最大值 + 1（用户自建模板排在内置后）
        try:
            existing_orders = [
                int(t.get("order", 0)) for t in self.list_templates()
                if isinstance(t.get("order"), (int, float))
            ]
            tmpl_meta["order"] = (max(existing_orders) if existing_orders else 0) + 1
        except Exception:
            tmpl_meta["order"] = 999

        # 4. 归一化 charm_layers：实例存的是 dict-of-lists，模板要求 4 元素 list
        #    （历史遗留：前端 AttrTab 用 dict 形态编辑，模板 schema 用平 list；这里转一次）
        if "charm_layers" in tmpl_meta:
            tmpl_meta["charm_layers"] = _normalize_charm_layers_to_template_format(
                tmpl_meta["charm_layers"]
            )

        # 5. v2 schema 校验（要 raise 在写盘前）
        meta_errors = _validate_template_meta(tmpl_meta)
        if meta_errors:
            raise TemplateValidationError(meta_errors)

        # 6. 写盘 — 原子性：先写临时目录再 rename
        target_dir.mkdir(parents=True, exist_ok=True)

        # 复制人格定义文件（始终带）
        ALWAYS_INCLUDE = ("GENOME.md", "IDENTITY.md", "SOUL.md", "PERSONALITY.md")
        for fname in ALWAYS_INCLUDE:
            src_file = src_config.agent_dir / fname
            if src_file.exists():
                shutil.copy2(src_file, target_dir / fname)

        # 头像：自定义头像优先（custom_avatar.png），否则用 avatar.png
        for cand in ("custom_avatar.png", "avatar.png"):
            src_avatar = src_config.agent_dir / cand
            if src_avatar.exists():
                shutil.copy2(src_avatar, target_dir / "avatar.png")
                break

        # 可选：带记忆
        if include_memory:
            mem_src = src_config.long_term_memory_file
            if mem_src.exists():
                shutil.copy2(mem_src, target_dir / "MEMORY.md")

        # 可选：带 USER.md
        if include_user:
            user_src = src_config.agent_dir / "USER.md"
            if user_src.exists():
                shutil.copy2(user_src, target_dir / "USER.md")

        # 写 meta.json（v2 格式）
        (target_dir / "meta.json").write_text(
            json.dumps(tmpl_meta, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )

        # 6. 补 avatar_url 给前端用（list_templates 里也会动态补，这里返回时一并带上）
        avatar_png = target_dir / "avatar.png"
        if avatar_png.exists():
            try:
                mtime = int(avatar_png.stat().st_mtime)
            except OSError:
                mtime = 0
            tmpl_meta["avatar_url"] = f"/agents/templates/{template_id}/avatar?v={mtime}"

        return tmpl_meta

    def update_agent(self, agent_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """更新智能体信息

        支持更新: name, description, avatar, order, prototype,
                 tags, spectrum, charm_layers, 以及配置文件 (identity, soul, personality)
        """
        config = self._get_config(agent_id)
        if not config.agent_dir.exists():
            return None

        meta = config.load_meta()

        # 更新基本字段
        for field in ['name', 'description', 'avatar', 'order',
                      'prototype', 'tags', 'spectrum', 'charm_layers']:
            if field in updates:
                meta[field] = updates[field]

        # 更新配置文件（兼容旧接口：直接传字符串内容）
        if 'identity' in updates:
            config.write_file(config.identity_file, updates['identity'])
        if 'soul' in updates:
            config.write_file(config.soul_file, updates['soul'])
        if 'personality' in updates:
            config.write_file(config.personality_file, updates['personality'])

        # 如果更新了名称或描述，同步更新 IDENTITY.md（如果为空）
        if 'name' in updates or 'description' in updates:
            current_identity = config.read_file(config.identity_file)
            if not current_identity:
                identity_content = DEFAULT_IDENTITY.format(
                    name=meta.get('name', ''),
                    description=meta.get('description', '')
                )
                config.write_file(config.identity_file, identity_content)

        config.save_meta(meta)
        self._configs.pop(agent_id, None)
        return self.get_agent(agent_id)

    def delete_agent(self, agent_id: str) -> bool:
        """删除智能体（整个目录）"""
        config = self._get_config(agent_id)
        if not config.agent_dir.exists():
            return False

        # 不允许删除 default
        if agent_id == "default":
            return False

        shutil.rmtree(config.agent_dir)
        self._configs.pop(agent_id, None)
        return True

    def get_agent_config(self, agent_id: str) -> Optional[AgentConfig]:
        """获取智能体配置对象"""
        config = self._get_config(agent_id)
        if not config.agent_dir.exists():
            return None
        return config

    def get_allowed_tools(self, agent_id: str) -> Optional[list]:
        """读 agent meta.json 里的 'tools' 白名单字段。

        返回值语义：
        - None：未配置 / agent 不存在 / 配置损坏 → 不过滤（保持向后兼容，主对话仍拿全工具）
        - list：白名单（仅这些工具的 schema 进 LLM system prompt，节省 prompt token）
        - []：空白名单（工具全部禁用——边界场景，调用方自行决定是否覆盖）

        设计说明：
        - 读 meta.json 而非新建 tools.json：减少额外 IO + 跟现有"血统/模板"管理同源
        - 失败时静默返回 None：白名单读失败不能阻断对话；最差是多花点 prompt token
        - 不在这里做 deny-list 逻辑：保持简单，复杂权限交给 Guardian
        """
        try:
            config = self._get_config(agent_id)
            if not config.meta_file.exists():
                return None
            meta = config.load_meta()
            tools = meta.get("tools")
            if tools is None:
                return None
            tools_list = [str(t) for t in tools]
            # 兼容性自动补全：若白名单包含基础文件/搜索工具但缺少 fast_context_search，自动补全并写回
            if "fast_context_search" not in tools_list and ("grep" in tools_list or "read_file" in tools_list):
                tools_list.append("fast_context_search")
                try:
                    meta["tools"] = tools_list
                    config.save_meta(meta)
                except Exception:
                    pass
            return tools_list
        except Exception:
            return None

    def migrate_default(self):
        """初始化默认智能体（柳如烟）：从 templates/liuruyan/ 模板实例化
        
        行业标准：模板层（冻结）→ 实例层（可变）
        - GENOME/SOUL/IDENTITY/PERSONALITY 等人格定义来自模板
        - MEMORY/EMOTION/memory/ 等运行时数据不受影响
        - 已存在的实例只做血统修复（补 source_template 字段），不覆盖运行时数据
        """
        DEFAULT_TEMPLATE_NAME = "liuruyan"
        template_dir = self.templates_dir / DEFAULT_TEMPLATE_NAME
        config = self._get_config("default")

        is_fresh_install = not config.meta_file.exists()
        needs_bloodline_fix = False

        if not is_fresh_install:
            # 实例已存在 — 检查血统是否正确记录
            meta = config.load_meta()
            if not meta.get("source_template") or meta.get("source_template") == "default":
                needs_bloodline_fix = True
            
            # 血统修复：补全 source_template 字段，同步缺失的模板文件
            if needs_bloodline_fix:
                logger.info("[AGENT] repairing default agent bloodline...")
                self._repair_default_bloodline(config)
            return  # 已迁移且血统正常（或刚修复完），跳过

        config.ensure_dirs()

        # === 从模板复制人格定义文件（只读层）===
        TEMPLATE_FILES_TO_COPY = [
            ("GENOME.md", config.genome_file),
            ("SOUL.md", config.soul_file),
            ("IDENTITY.md", config.identity_file),
            ("PERSONALITY.md", config.personality_file),
        ]

        if template_dir.exists():
            for src_name, dst_path in TEMPLATE_FILES_TO_COPY:
                src = template_dir / src_name
                if src.exists() and not dst_path.exists():
                    shutil.copy2(src, dst_path)

            # 复制头像（如果有）
            avatar_src = template_dir / "avatar.png"
            if avatar_src.exists():
                shutil.copy2(avatar_src, config.agent_dir / "avatar.png")

            # 从模板 meta.json 读取版本信息
            tmpl_meta = {}
            tmpl_meta_file = template_dir / "meta.json"
            if tmpl_meta_file.exists():
                try:
                    tmpl_meta = json.loads(tmpl_meta_file.read_text(encoding='utf-8-sig'))
                except Exception:
                    pass
            tmpl_version = tmpl_meta.get("meta", {}).get("version", 1)
            tmpl_avatar = tmpl_meta.get("avatar", "🌸")
        else:
            # 模板不存在时降级为默认内容（不应触发）
            logger.warning("[AGENT] template '%s' not found, using fallback", DEFAULT_TEMPLATE_NAME)
            tmpl_version = 1
            tmpl_avatar = "🌸"
            if not config.soul_file.exists():
                config.write_file(config.soul_file, DEFAULT_SOUL)
            if not config.identity_file.exists():
                identity_content = DEFAULT_IDENTITY.format(name="柳如烟", description="温柔灵性，愿为老板赴汤蹈火…")
                config.write_file(config.identity_file, identity_content)
            if not config.personality_file.exists():
                config.write_file(config.personality_file, DEFAULT_PERSONALITY)

        # === 创建运行时文件（可变层）===
        if not config.emotion_file.exists():
            config.save_emotion(DEFAULT_EMOTION.copy())

        if not config.long_term_memory_file.exists():
            config.write_file(config.long_term_memory_file, "# 柳如烟的记忆\n\n*每一次对话都是新的开始。*\n")

        # USER.md 默认：已存在则不动（保护老用户现有的“老板”档案），不存在才落盘默认空白笔记本
        user_md = config.agent_dir / "USER.md"
        if not user_md.exists():
            config.write_file(user_md, DEFAULT_USER_MD)

        # 迁移旧版对话历史和日记（兼容性）
        root = self.base_dir
        old_history = root / "memory" / "conversation_history.json"
        if old_history.exists() and not config.conversation_history_file.exists():
            shutil.copy2(old_history, config.conversation_history_file)

        old_memory_dir = root / "memory"
        if old_memory_dir.exists():
            for f in old_memory_dir.glob("*.md"):
                if f.stem != "conversation_history" and not (config.memory_dir / f.name).exists():
                    shutil.copy2(f, config.memory_dir / f.name)

        # === 写入元数据（带完整血统记录）===
        meta = {
            "id": "default",
            "name": "柳如烟",
            "description": "温柔灵性，愿为老板赴汤蹈火…",
            "avatar": tmpl_avatar,
            "source_template": DEFAULT_TEMPLATE_NAME,
            "template": DEFAULT_TEMPLATE_NAME,          # 兼容字段：指向实际模板名
            "source_version": str(tmpl_version),
            "created_from": "migrate_default",
            "created_at": datetime.now().isoformat(),
            "order": 0,
            # 脐带字段：记录模板来源和同步状态
            "template_sync": {
                "genome_inherit": True,
                "last_synced_version": tmpl_version,
            },
        }
        config.save_meta(meta)

    def _repair_default_bloodline(self, config: AgentConfig):
        """修复已存在的 default 实例的脏数据（不覆盖运行时数据）
        
        只做三件事：
        1. 修正 meta.json 血统字段
        2. 从模板补全缺失的人格文件（GENOME/SOUL等）
        3. 绝不碰 MEMORY/EMOTION/memory/ 等运行时数据
        """
        DEFAULT_TEMPLATE_NAME = "liuruyan"
        template_dir = self.templates_dir / DEFAULT_TEMPLATE_NAME

        # === 1. 读取当前 meta，修正血统字段 ===
        meta = config.load_meta()

        # 读取模板版本信息
        tmpl_version = 1
        if template_dir.exists():
            tmpl_meta_file = template_dir / "meta.json"
            if tmpl_meta_file.exists():
                try:
                    tmpl_meta = json.loads(tmpl_meta_file.read_text(encoding='utf-8-sig'))
                    tmpl_version = tmpl_meta.get("meta", {}).get("version", 1)
                except Exception:
                    pass

        # 修血统
        old_template = meta.get("template", "")
        changes = []
        if not meta.get("source_template") or meta.get("source_template") == "default":
            meta["source_template"] = DEFAULT_TEMPLATE_NAME
            changes.append(f"source_template → {DEFAULT_TEMPLATE_NAME}")
        
        if old_template == "default" or not old_template:
            meta["template"] = DEFAULT_TEMPLATE_NAME
            changes.append(f"template: '{old_template}' → {DEFAULT_TEMPLATE_NAME}")

        # 补充或更新脐带字段
        meta["template_sync"] = {
            "genome_inherit": True,
            "last_synced_version": tmpl_version,
        }
        meta["source_version"] = str(tmpl_version)
        if "created_from" not in meta:
            meta["created_from"] = "migrate_default (repaired)"

        config.save_meta(meta)
        logger.info("[AGENT] bloodline repaired: %s", '; '.join(changes))

        # === 2. 从模板补全缺失的人格文件（不覆盖已存在的）===
        if template_dir.exists():
            template_files = {
                "GENOME.md": config.genome_file,
                "SOUL.md": config.soul_file,
                "IDENTITY.md": config.identity_file,
                "PERSONALITY.md": config.personality_file,
            }
            for src_name, dst_path in template_files.items():
                src = template_dir / src_name
                if src.exists() and not dst_path.exists():
                    shutil.copy2(src, dst_path)
                    logger.info("[AGENT] synced from template: %s", src_name)
            
            # 头像也补一下
            avatar_src = template_dir / "avatar.png"
            avatar_dst = config.agent_dir / "avatar.png"
            if avatar_src.exists() and not avatar_dst.exists():
                shutil.copy2(avatar_src, avatar_dst)


# ── 模板 Schema v2 校验 / 反向保存（角色实例 → 模板）──────────────────────────
#
# 设计契约（与 agents/templates/SCHEMA.md 对齐）：
# - 出厂内置模板 ID 列表硬编码（与 backend/social_graph.py DEFAULT_RELATIONSHIPS 同源），
#   `save_as_template` 不允许覆盖这些——属于"出厂资产"，被覆盖很难恢复
# - 必填字段缺失直接抛 TemplateValidationError，前端可拿 errors 列表引导用户去 Workshop 补全
# - 实例级运行时字段统一剔除：脐带 / emotion / 模型路由 / 工具白名单等都不进模板
# - MEMORY.md / USER.md 默认不带（含具体场景与"老板"个人化内容，跨实例继承会污染）

BUILTIN_TEMPLATE_IDS = frozenset({
    "liuruyan", "hanqingxue", "xiawanling", "shenweilan",
    "qingyuan", "guwenshu", "xiaohandu", "luchengfeng",
    "fushanyue", "chuyelan",
})

# 实例 meta.json 里需要从模板 meta.json 中剔除的字段（v2 schema 不要这些）
_TEMPLATE_INSTANCE_FIELDS_TO_DROP = frozenset({
    "id", "created_at", "template", "source_template",
    "template_version", "template_born_at", "template_sync",
    "source_version", "created_from",
    "emotion", "emotion_intensity",
    "avatar_url", "avatar_template",
    "tools", "active_model",
})

# Schema v2 spectrum 7 维（顺序无关，只校验键集合）
_REQUIRED_SPECTRUM_DIMS = frozenset({
    "warmth", "intelligence", "loyalty", "charm",
    "independence", "vulnerability", "expressiveness",
})

# Schema v2 必填顶层字段（不含 meta 子字段；meta 单独校验）
# 注：one_line 早期设计为"角色宣传口号"，但前后端都无展示位 / 不进 LLM 提示词，
# 于 2026-05 清理从必填中移除；老模板 meta.json 里保留字段文本不受影响。
_REQUIRED_TEMPLATE_TOP_FIELDS = (
    "name", "prototype", "gender", "description",
    "core", "avatar", "charm_layers", "spectrum",
)

# template_id 格式：以小写字母开头，仅含小写字母 / 数字 / 下划线，2-32 字符
import re as _re
_TEMPLATE_ID_RE = _re.compile(r"^[a-z][a-z0-9_]{1,31}$")


class TemplateValidationError(Exception):
    """Schema v2 校验失败 — errors 是给前端展示的人话清单"""

    def __init__(self, errors: list):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors) if self.errors else "template validation failed")


def _validate_template_id(template_id: str) -> list:
    errors = []
    if not template_id or not isinstance(template_id, str):
        errors.append("template_id 不能为空")
        return errors
    if not _TEMPLATE_ID_RE.match(template_id):
        errors.append("template_id 必须以小写字母开头，仅含小写字母/数字/下划线，长度 2-32 字符")
    return errors


def _normalize_charm_layers_to_template_format(cl: Any) -> Any:
    """归一化 charm_layers：实例的 dict 形态 → 模板的 list 形态。

    历史遗留：
    - 模板（templates/<id>/meta.json）格式：4 元素字符串数组
        ["层1描述", "层2描述", "层3描述", "层4描述"]
    - 实例（agents/<id>/meta.json）格式：dict-of-lists（前端 AttrTab 编辑结果）
        {"layer_1": ["..."], "layer_2": ["..."], "layer_3": [...], "layer_4": [...]}

    save_as_template 在校验前调一次这个函数，把实例形态归一化为模板形态。
    多条 charm 描述用 \\n 拼接，不丢用户内容。
    """
    if isinstance(cl, list):
        return cl  # 已经是模板格式，直接返回
    if isinstance(cl, dict):
        # 按 key 排序（layer_1 / layer_2 / layer_3 / layer_4 自然有序）
        result = []
        for key in sorted(cl.keys()):
            v = cl[key]
            if isinstance(v, list):
                # 多条魅力描述用 \n 拼接，保留全部信息
                joined = "\n".join(s for s in v if isinstance(s, str) and s.strip())
                if joined:
                    result.append(joined)
            elif isinstance(v, str) and v.strip():
                result.append(v)
        return result
    return cl  # 其他奇怪形态原样返回，让校验抛错


def _validate_template_meta(meta: Dict[str, Any]) -> list:
    """对照 agents/templates/SCHEMA.md v2 校验 meta 结构。

    返回缺失/不合法字段的人话清单（list[str]）。空列表表示校验通过。
    宽松点：prototype 不限制必须是 10 大原型之一（用户可自创），但要求非空字符串。
    """
    errors = []
    for field in _REQUIRED_TEMPLATE_TOP_FIELDS:
        if field not in meta or meta[field] in (None, ""):
            errors.append(f"缺少必填字段：{field}")

    # gender 取值
    gender = meta.get("gender")
    if gender and gender not in ("female", "male", "other"):
        errors.append(f"gender 必须是 female/male/other 之一（当前：{gender}）")

    # charm_layers 必须 4 元素字符串数组
    cl = meta.get("charm_layers")
    if cl is not None:
        if not isinstance(cl, list) or len(cl) != 4:
            errors.append(f"charm_layers 必须是 4 元素数组（当前长度：{len(cl) if isinstance(cl, list) else 'N/A'}）")
        elif not all(isinstance(x, str) and x.strip() for x in cl):
            errors.append("charm_layers 每一层必须是非空字符串")

    # spectrum 必须含且仅含 7 维
    spec = meta.get("spectrum")
    if spec is not None:
        if not isinstance(spec, dict):
            errors.append("spectrum 必须是对象（含 7 个数值维度）")
        else:
            spec_keys = set(spec.keys())
            missing = _REQUIRED_SPECTRUM_DIMS - spec_keys
            extra = spec_keys - _REQUIRED_SPECTRUM_DIMS
            if missing:
                errors.append(f"spectrum 缺少维度：{', '.join(sorted(missing))}")
            if extra:
                errors.append(f"spectrum 含未知维度：{', '.join(sorted(extra))}")
            for k, v in spec.items():
                if k in _REQUIRED_SPECTRUM_DIMS:
                    if not isinstance(v, (int, float)) or not (0.0 <= float(v) <= 1.0):
                        errors.append(f"spectrum.{k} 必须是 0.0-1.0 之间的数值")

    # avatar 必须是字符串（v2 不接受 base64 / 文件路径）
    avatar = meta.get("avatar")
    if avatar and not isinstance(avatar, str):
        errors.append("avatar 必须是字符串（emoji 或文字）")
    elif isinstance(avatar, str) and avatar.startswith("data:"):
        errors.append("avatar 不允许使用 base64 data URL，请改用 emoji")

    return errors


# ── 角色级 active_model 读写（per-agent model routing 阶段 1 Commit 1.3）─────
#
# 设计：
# - 存储位置：agent 自己的 meta.json 顶层 `active_model` 字段
#   形如：{"provider_id": "deepseek", "model": "deepseek-chat-v3.1"}
# - 缺失/空 → 该 agent 走全局默认 model（user_rules 里 Q3 决议：保留全局 fallback）
# - 直接读 meta.json 而非走 AgentManager.get_agent，是为了避开 migrate_default
#   等首次 init 副作用——这两个函数会在 LLM 调用热路径上每次调
# - 写后不主动清缓存：AgentManager 自身 _configs 缓存的是 AgentConfig 对象（路径），
#   不缓存 meta，所以无需失效
#
# 调用方契约：
# - `get_agent_active_model` 返回 None 表示"未配置"，调用方应继续走下一级 fallback
# - `set_agent_active_model(model=None/"")` 清除字段（让 agent 回到全局默认）


def _agent_meta_path(agent_id: str) -> Path:
    from . import paths
    return paths.data_dir() / "agents" / agent_id / "meta.json"


def get_agent_active_model(agent_id: Optional[str]) -> Optional[Dict[str, str]]:
    """读 agent meta.json 的 active_model 字段（per-call 热路径调用，无副作用）。

    Returns:
        {"provider_id": str, "model": str} 或 None（未配置 / 不存在 / 损坏）
    """
    if not agent_id:
        return None
    meta_path = _agent_meta_path(agent_id)
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding='utf-8-sig'))
    except Exception:
        return None
    active = meta.get("active_model")
    if not isinstance(active, dict):
        return None
    model = active.get("model")
    if not model:
        return None
    return {
        "provider_id": str(active.get("provider_id") or ""),
        "model": str(model),
    }


def set_agent_active_model(
    agent_id: str,
    provider_id: Optional[str],
    model: Optional[str],
) -> bool:
    """写 agent meta.json 的 active_model 字段。

    Args:
        agent_id: 角色 id
        provider_id: 来自 providers store 的 ProviderConfig.id（如 "deepseek"）
        model: model id；传 None / "" 表示清除（让该 agent 回到全局默认）

    Returns:
        True 写入成功；False 当 agent 不存在或 meta 损坏
    """
    if not agent_id:
        return False
    meta_path = _agent_meta_path(agent_id)
    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding='utf-8-sig'))
    except Exception:
        return False
    if not model:
        meta.pop("active_model", None)
    else:
        meta["active_model"] = {
            "provider_id": str(provider_id or ""),
            "model": str(model),
        }
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    return True


# 全局管理器实例
_agent_manager: Optional[AgentManager] = None


def get_agent_manager() -> AgentManager:
    """获取智能体管理器单例"""
    global _agent_manager
    if _agent_manager is None:
        _agent_manager = AgentManager()
        # 首次初始化时迁移 default
        _agent_manager.migrate_default()
    return _agent_manager
