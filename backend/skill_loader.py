"""
技能系统 - 全局技能池 + 角色装备槽

架构:
- 全局技能池: skills/ 目录下的所有 SKILL.md，作为可装备的技能仓库
- 角色装备槽: 每个 agent 的 meta.json 中 equipped_skills 字段，记录已装备的技能名列表
- 动态装备: 用户可随时给角色装备/卸载技能，灵活组合

注入逻辑:
- 有装备技能的角色: 只注入已装备的技能索引
- 无装备技能的角色: 注入全局所有技能（向后兼容）
"""
import logging
import os
import re
import json
import yaml
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass

from . import paths

logger = logging.getLogger(__name__)


# Plugin pack 命名空间分隔符。
# 选 `__` 而非 `:` 是因为 FastAPI 路径参数 / 文件名 / shell 转义都安全。
# 现有 39 个单层 skill 名字不含此分隔符，零冲突。
PACK_NAME_SEP = "__"

# Pack agent (subagent) 命名空间分隔符。
# 与 skill 不同，subagent name 只出现在工具参数字符串里，从不进 URL/文件名。
# 用 `:` 是为了 100% 对齐 Claude Code 协议（SKILL.md 里写的是
# `Agent(subagent_type="webnovel-writer:reviewer")`，字符串原样可用）。
PACK_AGENT_NAME_SEP = ":"


# Claude Code agents/*.md 的 `tools:` 字段使用的是 Anthropic 标准工具名；
# 柳如烟内部工具名（registry 注册的）部分不同。这张表把 Claude Code 名映射到
# 柳如烟名。未在表里的工具名按原样保留并记 warning（未来若柳如烟新加同名工具，
# pack agent 仍能用上）。
#
# 映射来源：核对 webnovel-writer/agents/*.md 与柳如烟 clean_registry 实际注册的工具名。
CLAUDE_TO_LIURUYAN_TOOL_MAP: Dict[str, str] = {
    # 文件读写
    "Read": "read_file",
    "Write": "write_file",
    "Edit": "edit",
    "MultiEdit": "edit",
    # 搜索
    "Grep": "grep",
    "Glob": "glob_search",
    # 命令执行
    "Bash": "execute_command",
    # 网络
    "WebSearch": "web_search",
    "WebFetch": "web_extract",
    # 子代理 / 交互
    "Agent": "spawn_subagent",
    "Task": "spawn_subagent",  # Claude Code 旧名
    "AskUserQuestion": "ask_user_question",
    # 无对应工具（暂跳过；future 可补）
    # "TodoWrite": None,
    # "NotebookEdit": None,
}


def map_claude_tools_to_liuruyan(claude_tools: List[str]) -> List[str]:
    """把 Claude Code agents/*.md frontmatter 的 tools 列表映射成柳如烟工具名。

    未识别的工具名按原样保留（柳如烟未来若注册同名工具自动可用），
    不在 map 也不是柳如烟原生工具的会被 SubAgentEngine 自然忽略。
    """
    result: List[str] = []
    for raw in claude_tools or []:
        raw = (raw or "").strip()
        if not raw:
            continue
        mapped = CLAUDE_TO_LIURUYAN_TOOL_MAP.get(raw, raw)
        if mapped and mapped not in result:
            result.append(mapped)
    return result


@dataclass
class Skill:
    """技能定义"""
    name: str
    description: str
    content: str
    path: Path
    disabled: bool = False
    category: str = ""       # 技能分类（如"开发"、"写作"、"分析"）
    tags: List[str] = None   # 标签，便于搜索和推荐
    display_name: str = ""  # 展示名（中文），为空时回退到 name
    # ── plugin pack 来源信息（None = 单层 skill，非 pack 来源）─────────
    pack_name: Optional[str] = None      # 所属 pack 目录名（如 "webnovel-writer"）
    pack_dir: Optional[Path] = None      # pack 根目录绝对路径（含 .claude-plugin/）


@dataclass
class PackAgent:
    """Pack 装来的 sub-agent 定义（解析自 <pack>/agents/*.md）。

    对齐 Claude Code agents/*.md 协议——frontmatter 含 name/description/tools/model，
    body 是给 LLM 的角色 prompt。SkillLoader 把这些 PackAgent 暴露成
    SUBAGENT_TEMPLATES 格式给 SubAgentEngine 用，让 LLM 通过
    `spawn_subagent(agent_types=["<pack>:<agent>"], task=...)` 调起来。
    """
    name: str                            # 原始 name（无 namespace 前缀），如 "reviewer"
    qualified_name: str                  # 带 namespace，如 "webnovel-writer:reviewer"
    pack_name: str                       # 所属 pack 目录名
    pack_dir: Path                       # pack 根目录
    description: str = ""
    body: str = ""                       # 完整 prompt 正文（去掉 frontmatter 后的 .md 内容）
    tools_hint: List[str] = None         # 已映射成柳如烟工具名的白名单
    model: str = "inherit"               # Claude Code 协议：通常 "inherit" / "sonnet" / "haiku"


class SkillLoader:
    """技能加载器 — 全局技能池 + 角色装备映射"""

    def __init__(self, skills_dir: str = None):
        if skills_dir is None:
            self.skills_dir = paths.skills_dir()
        else:
            self.skills_dir = Path(skills_dir)
        self.agents_dir = paths.agents_dir()
        self.skills: Dict[str, Skill] = {}
        # Pack 装来的 sub-agent: qualified_name (e.g. "webnovel-writer:reviewer") -> PackAgent
        # 装备/卸载 pack 时通过 reload() 重新填充——LLM 看到的 spawn_subagent 可用类型
        # 是 SUBAGENT_TEMPLATES 原生 3 个 + 这里的 pack agents 合并视图（动态）。
        self.pack_agents: Dict[str, PackAgent] = {}
        # 角色装备映射: agent_id -> List[skill_name]
        self._equipped_cache: Dict[str, List[str]] = {}
        self._load_all()

    def _parse_skill(self, file_path: Path) -> Optional[Skill]:
        """解析技能文件"""
        try:
            content = file_path.read_text(encoding='utf-8')

            # 解析 YAML frontmatter
            if content.startswith('---'):
                parts = content.split('---', 2)
                if len(parts) >= 3:
                    frontmatter = yaml.safe_load(parts[1])
                    body = parts[2].strip()

                    return Skill(
                        name=frontmatter.get('name', file_path.parent.name),
                        description=frontmatter.get('description', ''),
                        content=body,
                        path=file_path,
                        disabled=frontmatter.get('disable', False),
                        category=frontmatter.get('category', ''),
                        tags=frontmatter.get('tags', []),
                        display_name=frontmatter.get('display_name', ''),
                    )

            # 没有 frontmatter，使用文件名
            return Skill(
                name=file_path.stem,
                description='',
                content=content,
                path=file_path,
                category='',
                tags=[],
                display_name='',
            )
        except Exception as e:
            logger.error("Error parsing skill %s: %s", file_path, e)
            return None

    def _load_all(self):
        """加载所有技能（全局技能池）

        支持两种 skill 来源：
        1. **单层 skill**（现有）：`skills/<name>/SKILL.md`
           - 现有 39 个 skill 全走这条；零行为变化
        2. **plugin pack**（2026-05-26 新增）：`skills/<pack>/.claude-plugin/plugin.json`
           - 兼容 Anthropic Claude Code Plugin 协议
           - 扫 `<pack>/skills/<skill>/SKILL.md`，name 加 `<pack>__` 前缀
           - pack_dir 写入 Skill 对象，工具调用时可用作脚本路径基址
        """
        if not self.skills_dir.exists():
            return

        for skill_dir in self.skills_dir.iterdir():
            if not skill_dir.is_dir():
                continue
            # 路径 1：单层 skill（兼容现有）
            skill_file = skill_dir / "SKILL.md"
            if skill_file.exists():
                skill = self._parse_skill(skill_file)
                if skill and not skill.disabled:
                    self.skills[skill.name] = skill
                continue
            # 路径 2：plugin pack。优先 .liuruyan-pack.json（柳如烟内部元信息，里面
            # 记录了 plugin_root_relative——wrapper repo 场景下 plugin.json 在子目录。
            # 其次才側金 .claude-plugin/plugin.json 贴边检测（手动 clone 进来的）。
            meta_file = skill_dir / ".liuruyan-pack.json"
            if meta_file.exists():
                self._load_plugin_pack_via_meta(skill_dir, meta_file)
                continue
            plugin_manifest = skill_dir / ".claude-plugin" / "plugin.json"
            if plugin_manifest.exists():
                self._load_plugin_pack(skill_dir, plugin_root=skill_dir)
                continue
            # 其它目录（无 SKILL.md / 无 plugin.json / 无 .liuruyan-pack.json）：静默跳过
            # 比如 .git / .venv / scripts 等 pack 内部子目录；或用户自建空目录

    def _load_plugin_pack_via_meta(self, pack_dir: Path, meta_file: Path) -> None:
        """走 .liuruyan-pack.json 路径。meta 中 plugin_root_relative 可能是 "." 或
        子目录名（wrapper repo）。失败后退回 skill_dir 本体。"""
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            logger.warning("[skill_loader] 读 %s 失败，忽略: %s", meta_file, exc)
            return
        rel = meta.get("plugin_root_relative") or "."
        plugin_root = (pack_dir / rel).resolve()
        if not plugin_root.exists():
            logger.warning("[skill_loader] %s 的 plugin_root 丢失: %s", pack_dir.name, plugin_root)
            return
        # 校验 plugin.json 存在（避免 meta 有但包被微微损坏）
        if not (plugin_root / ".claude-plugin" / "plugin.json").exists():
            logger.warning("[skill_loader] %s 下未找到 .claude-plugin/plugin.json", plugin_root)
            return
        self._load_plugin_pack(pack_dir, plugin_root=plugin_root)

    def _load_plugin_pack(self, pack_dir: Path, plugin_root: Optional[Path] = None) -> None:
        """加载一个 Claude Code Plugin pack 内的所有 skill。

        约定：
        - pack_dir = 根目录（skills/<pack>/），用于 pack_name 和 venv 探测
        - plugin_root = .claude-plugin/ 所在目录；wrapper repo 下可与 pack_dir 不同
          例：pack_dir = `skills/webnovel-writer/`
             plugin_root = `skills/webnovel-writer/webnovel-writer/`
          默认与 pack_dir 同。
        - pack 内 `<plugin_root>/skills/<name>/SKILL.md` 被识别
        - 加载后 skill.name = `<pack_name>__<original_name>`（避免冲突）
        - skill.content 末尾追加 pack 路径信息块（PACK_DIR / PACK_PYTHON / PACK_SCRIPTS）
          让 LLM 调 execute_command 时知道脚本位置
        - skill.pack_name / skill.pack_dir 记录来源（pack_dir 根、不是 plugin_root）

        失败兜底：单个 skill 解析失败不影响其他；pack 目录格式异常静默跳过。
        """
        pack_name = pack_dir.name
        if plugin_root is None:
            plugin_root = pack_dir

        # 先发现 pack agents（让 inject_pack_context 渲染「平台适配提示」时
        # 能列出本 pack 内可调 agents）。agents/ 不存在时该方法静默 no-op。
        self._discover_pack_agents(pack_dir, plugin_root)

        pack_skills_dir = plugin_root / "skills"
        # 2026-05-27：之前这里 pack 没 skills/ 就 return，导致只装 agents/ 的 pack 完全
        # 不可见。现在改为：skills/ 缺也继续走，因为 agents/ 是独立组件。
        if pack_skills_dir.exists() and pack_skills_dir.is_dir():
            for sub in pack_skills_dir.iterdir():
                if not sub.is_dir():
                    continue
                skill_file = sub / "SKILL.md"
                if not skill_file.exists():
                    continue
                skill = self._parse_skill(skill_file)
                if not skill or skill.disabled:
                    continue
                # 命名空间化：webnovel-writer__webnovel-init
                original_name = skill.name
                skill.name = f"{pack_name}{PACK_NAME_SEP}{original_name}"
                skill.pack_name = pack_name
                skill.pack_dir = pack_dir
                skill.content = self._inject_pack_context(
                    skill.content, pack_dir, plugin_root, pack_name
                )
                self.skills[skill.name] = skill

    def _inject_pack_context(
        self,
        content: str,
        pack_dir: Path,
        plugin_root: Path,
        pack_name: str,
    ) -> str:
        """在 skill body 处理两件事：
        1. 装载时占位符替换：`${CLAUDE_PLUGIN_ROOT}` → 实际 plugin_root 字符串路径
           （SKILL.md 内部派生的 `${SCRIPTS_DIR}` / `${SKILL_ROOT}` 是 bash 派生的，
           不在装载时替换；运行时 LLM 跑 bash 时 export 出来即可）
        2. 末尾追加 pack 路径变量 + 平台适配提示（Agent → spawn_subagent）

        路径变量语义：
        - PACK_DIR    = pack 根（skills/<pack>/）——venv / .git / .liuruyan-pack.json 都在这里
        - PACK_PYTHON = pack 子 venv 的 python 解释器（未创建时指示 None）
        - PACK_SCRIPTS = plugin_root/scripts——wrapper repo 下 plugin_root 可能深一层

        不同操作系统 venv python 路径不一样，统一探测一次：
        - Windows: `<pack>/.venv/Scripts/python.exe`
        - Linux/macOS: `<pack>/.venv/bin/python`

        探测失败（venv 不存在）→ PACK_PYTHON 标记为未创建，LLM 看到提示后会改用系统 python。
        """
        # ── 1. 装载时占位符替换 ──
        # ${CLAUDE_PLUGIN_ROOT} 是 Claude Code 协议变量，SKILL.md 大量引用它来派生
        # SCRIPTS_DIR / SKILL_ROOT。在柳如烟没有 shell 注入机制，因此装载时直接做
        # 文本替换最稳。其他用户上下文相关占位符（${PROJECT_ROOT} / ${WORKSPACE_ROOT}
        # / ${CHAPTER_FILE} 等）不动——让 LLM 在调用时按当前小说项目实际路径填入。
        #
        # SKILL.md 实际有两种引用形式：
        #   - `${CLAUDE_PLUGIN_ROOT}` 普通引用
        #   - `${CLAUDE_PLUGIN_ROOT:?}` bash 默认值语法"未设置则报错"
        # 用正则一次性覆盖（含可选 :modifier 部分）。
        # 用 lambda 而非字符串作 repl：Windows 路径含 `\A` 等反斜杠序列，
        # 直接传字符串会被当作 backref template 解析报 `bad escape \A`。
        _plugin_root_str = str(plugin_root)
        content = re.sub(
            r"\$\{CLAUDE_PLUGIN_ROOT(?::[^}]*)?\}",
            lambda _m: _plugin_root_str,
            content,
        )

        # ── 2. 路径变量 suffix ──
        venv_win = pack_dir / ".venv" / "Scripts" / "python.exe"
        venv_unix = pack_dir / ".venv" / "bin" / "python"
        if venv_win.exists():
            pack_python = str(venv_win)
        elif venv_unix.exists():
            pack_python = str(venv_unix)
        else:
            pack_python = "<未创建子 venv，请用系统 python 或 pip install 后重新加载>"

        scripts_dir = plugin_root / "scripts"
        suffix = (
            "\n\n---\n\n"
            "## Plugin Pack 路径变量（柳如烟运行时注入）\n\n"
            f"- **PACK_DIR**: `{pack_dir}`\n"
            f"- **PLUGIN_ROOT**: `{plugin_root}`\n"
            f"- **PACK_PYTHON**: `{pack_python}`\n"
            f"- **PACK_SCRIPTS**: `{scripts_dir}`\n\n"
            "调用 pack 内 Python 脚本请使用 `execute_command` + 上述路径，例如：\n"
            f"`{pack_python} {scripts_dir / 'xxx.py'}`\n"
        )

        # ── 3. 平台适配提示（仅当 pack 装了 agents 时追加）──
        # SKILL.md 写的是 Claude Code 协议的 `Agent(subagent_type="<pack>:<name>")`，
        # 在柳如烟里要改用 `spawn_subagent(agent_types=["<pack>:<name>"], task=...)`。
        # 这段提示明确告诉 LLM 怎么对接，避免它按字面调一个不存在的 Agent() 工具。
        pack_agents_here = [
            a for a in self.pack_agents.values() if a.pack_name == pack_name
        ]
        if pack_agents_here:
            agent_lines = []
            for a in pack_agents_here:
                desc = (a.description or "").strip()
                if len(desc) > 100:
                    desc = desc[:97] + "..."
                agent_lines.append(f"- `{a.qualified_name}` — {desc}")
            agents_block = "\n".join(agent_lines)
            suffix += (
                "\n\n---\n\n"
                "## 平台适配说明（柳如烟运行时注入）\n\n"
                "本 SKILL.md 按 Claude Code 协议编写，本 pack 装载于柳如烟。子代理调用"
                "已自动适配 namespace 命名（与 Claude Code 完全对齐）：\n\n"
                "**SKILL.md 里的 Claude Code 写法** → **柳如烟工具调用**：\n\n"
                "```\n"
                "Agent(subagent_type=\"<pack>:<agent>\", prompt=\"...\")\n"
                "  ↓ 等价\n"
                "spawn_subagent(\n"
                "  agent_types=[\"<pack>:<agent>\"],\n"
                "  task=\"...\"   # ← 原 prompt 字段塞这里\n"
                ")\n"
                "```\n\n"
                f"**本 pack（`{pack_name}`）可调的子代理**：\n\n"
                f"{agents_block}\n\n"
                "占位符提示：\n"
                "- `${CLAUDE_PLUGIN_ROOT}` 装载时已替换为实际路径\n"
                "- `${SCRIPTS_DIR}` / `${SKILL_ROOT}` 由 SKILL.md 在 bash 块里 export，"
                "执行 bash 时 shell 会派生（不需柳如烟介入）\n"
                "- `${PROJECT_ROOT}` / `${WORKSPACE_ROOT}` / `${CHAPTER_FILE}` 等是"
                "**用户上下文**——你调 spawn_subagent 时必须在 task 字符串里写入"
                "用户当前小说项目的实际绝对路径（如 `D:\\\\AI_Project\\\\共生`），"
                "不要原样传 `${PROJECT_ROOT}` 字面量。\n"
            )

        return content + suffix

    # ====== Pack sub-agents（对齐 Claude Code agents/*.md 协议）======

    def _discover_pack_agents(self, pack_dir: Path, plugin_root: Path) -> None:
        """扫 <plugin_root>/agents/*.md，把每个 sub-agent 解析挂到 self.pack_agents。

        约定（Claude Code Plugin 协议）：
        - 每个 .md 是一个 sub-agent，frontmatter 含 name/description/tools/model
        - body 是该 sub-agent 的 system prompt（角色扮演说明）
        - 命名空间化：qualified_name = "<pack-name>:<agent-name>"
          —— 用 `:` 而非 `__` 是为了让 SKILL.md 里写的
          `Agent(subagent_type="webnovel-writer:reviewer")` 字符串原样可用。
        """
        agents_dir = plugin_root / "agents"
        if not agents_dir.exists() or not agents_dir.is_dir():
            return
        pack_name = pack_dir.name
        for entry in agents_dir.iterdir():
            if not entry.is_file() or entry.suffix.lower() != ".md":
                continue
            agent = self._parse_pack_agent(entry, pack_name=pack_name, pack_dir=pack_dir)
            if agent is not None:
                self.pack_agents[agent.qualified_name] = agent

    def _parse_pack_agent(
        self,
        md_path: Path,
        pack_name: str,
        pack_dir: Path,
    ) -> Optional["PackAgent"]:
        """解析单个 agents/<name>.md 文件成 PackAgent。

        失败兜底：解析失败返回 None，不影响其他 agents 加载。
        """
        try:
            raw = md_path.read_text(encoding="utf-8")
        except Exception as exc:
            logger.warning("[skill_loader] read pack agent failed: %s: %s", md_path, exc)
            return None

        # YAML frontmatter 解析（沿用 _parse_skill 同款逻辑）
        name = md_path.stem
        description = ""
        body = raw
        tools_claude: List[str] = []
        model = "inherit"

        if raw.startswith("---"):
            parts = raw.split("---", 2)
            if len(parts) >= 3:
                try:
                    fm = yaml.safe_load(parts[1]) or {}
                except Exception as exc:
                    logger.warning("[skill_loader] yaml frontmatter parse failed for %s: %s", md_path, exc)
                    fm = {}
                body = parts[2].strip()
                name = (fm.get("name") or name).strip()
                description = (fm.get("description") or "").strip()
                model = (fm.get("model") or "inherit").strip()
                raw_tools = fm.get("tools")
                if isinstance(raw_tools, str):
                    # "Read, Grep, Bash" 也支持
                    tools_claude = [t.strip() for t in raw_tools.split(",") if t.strip()]
                elif isinstance(raw_tools, list):
                    tools_claude = [str(t).strip() for t in raw_tools if str(t).strip()]

        tools_hint = map_claude_tools_to_liuruyan(tools_claude)
        qualified = f"{pack_name}{PACK_AGENT_NAME_SEP}{name}"
        return PackAgent(
            name=name,
            qualified_name=qualified,
            pack_name=pack_name,
            pack_dir=pack_dir,
            description=description,
            body=body,
            tools_hint=tools_hint,
            model=model,
        )

    def get_pack_agent(self, qualified_name: str) -> Optional["PackAgent"]:
        """按 qualified_name（如 'webnovel-writer:reviewer'）取 pack agent。"""
        return self.pack_agents.get(qualified_name)

    def get_pack_subagent_templates(self) -> Dict[str, dict]:
        """返回 SUBAGENT_TEMPLATES 字典格式的 pack agents 视图。

        被 backend.subagent.templates.get_subagent_templates() 合并进原生模板，
        让 LLM 通过 spawn_subagent(agent_types=["webnovel-writer:reviewer"]) 调起来。

        每个 pack agent 转成的字典字段（与原生模板对齐）：
        - name: 显示名（用 qualified_name，便于前端区分）
        - icon: "review"（pack agent 统一暂用 review 图标；未来按 description 关键词推断）
        - system_prompt: agent body（角色扮演 prompt）
        - tools: 映射后的柳如烟工具白名单
        - disallowed_tools: 强制拒绝 spawn_* 防递归
        - timeout / max_tokens / max_tool_rounds / idle_timeout：保守默认值
        - model: agent.model（Claude Code 协议字段，柳如烟当前忽略，但保留供未来用）
        - thinking_config: "disabled"（pack agent 默认关推理；强 reasoning 模型上再放开）
        - source: "pack:<pack-name>"（前端/日志用，标识来源）
        """
        # 延迟 import 避免循环（subagent.templates import 我们）
        try:
            from .subagent.templates import _DEFAULT_DISALLOWED_TOOLS
        except Exception:
            _DEFAULT_DISALLOWED_TOOLS = [
                "spawn_subagent",
                "dispatch_batch", "ask_user_question",
                "cron_create", "cron_delete", "cron_set_enabled", "cron_list",
            ]

        out: Dict[str, dict] = {}
        for qname, agent in self.pack_agents.items():
            out[qname] = {
                "name": qname,
                "icon": "review",
                "system_prompt": agent.body,
                "tools": list(agent.tools_hint or []),
                "disallowed_tools": list(_DEFAULT_DISALLOWED_TOOLS),
                "timeout": 240,
                "max_tokens": 3000,
                "max_tool_rounds": 15,
                "idle_timeout": 45,
                "model": agent.model,
                "thinking_config": "disabled",
                "source": f"pack:{agent.pack_name}",
            }
        return out

    # ====== 角色装备管理 ======

    def get_equipped_skills(self, agent_id: str) -> List[str]:
        """获取角色已装备的技能列表"""
        # 先查缓存
        if agent_id in self._equipped_cache:
            return self._equipped_cache[agent_id]

        # 从 meta.json 读取
        equipped = self._load_equipped_from_meta(agent_id)
        self._equipped_cache[agent_id] = equipped
        return equipped

    def _load_equipped_from_meta(self, agent_id: str) -> List[str]:
        """从角色 meta.json 读取已装备技能"""
        meta_path = self.agents_dir / agent_id / "meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding='utf-8-sig'))
                return meta.get("equipped_skills", [])
            except Exception:
                pass
        return []

    def _save_equipped_to_meta(self, agent_id: str, skills: List[str]) -> bool:
        """保存已装备技能到角色 meta.json"""
        meta_path = self.agents_dir / agent_id / "meta.json"
        if not meta_path.exists():
            return False
        try:
            meta = json.loads(meta_path.read_text(encoding='utf-8-sig'))
            meta["equipped_skills"] = skills
            meta_path.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2),
                encoding='utf-8'
            )
            # 更新缓存
            self._equipped_cache[agent_id] = skills
            return True
        except Exception as e:
            logger.error("Error saving equipped skills for %s: %s", agent_id, e)
            return False

    def equip_skill(self, agent_id: str, skill_name: str) -> bool:
        """给角色装备技能"""
        # 验证技能存在
        if skill_name not in self.skills:
            return False

        equipped = self.get_equipped_skills(agent_id)
        if skill_name in equipped:
            return True  # 已装备

        equipped.append(skill_name)
        return self._save_equipped_to_meta(agent_id, equipped)

    def unequip_skill(self, agent_id: str, skill_name: str) -> bool:
        """给角色卸载技能"""
        equipped = self.get_equipped_skills(agent_id)
        if skill_name not in equipped:
            return True  # 本来就没装备

        equipped.remove(skill_name)
        return self._save_equipped_to_meta(agent_id, equipped)

    def set_equipped_skills(self, agent_id: str, skill_names: List[str]) -> bool:
        """批量设置角色装备的技能（覆盖）"""
        # 过滤不存在的技能
        valid_skills = [s for s in skill_names if s in self.skills]
        return self._save_equipped_to_meta(agent_id, valid_skills)

    # ====== 技能提示词生成 ======

    def get_skills_prompt(self, agent_id: str = None) -> str:
        """获取技能索引提示词

        逻辑:
        - 有 agent_id 且有装备技能: 只注入已装备的技能
        - 有 agent_id 但无装备技能: 注入全局所有技能（向后兼容）
        - 无 agent_id: 注入全局所有技能
        """
        if agent_id:
            equipped = self.get_equipped_skills(agent_id)
            if equipped:
                return self._build_prompt_for_skills(equipped)
            # 没有装备技能 → 兼容旧版，注入全局
            # 但加上提示：建议配置专属技能
            prompt = self._build_prompt_for_all()
            if prompt:
                prompt += "\n\n> 💡 你当前使用的是全局技能池。可以让老板为你装备专属技能，提升专业能力！"
            return prompt

        return self._build_prompt_for_all()

    def _build_prompt_for_skills(self, skill_names: List[str]) -> str:
        """为指定技能列表构建提示词。

        关键过滤：category=='discipline' 的条目不进索引——
        discipline 必须以全文形式由 agent.build_messages 在工作模式触发时直接注入；
        如果让它进了索引，模型会以为可以 load_skill 按需加载，但纪律是
        "决策瞬间必须在场"的行为约束，按需加载等于失效。
        """
        valid_skills = []
        for name in skill_names:
            skill = self.skills.get(name)
            if skill and skill.category != 'discipline':
                valid_skills.append(skill)

        if not valid_skills:
            return ""

        lines = ["## 可用技能索引\n"]
        lines.append("以下是你当前装备的专属技能。需要使用某个技能时，请先通过 load_skill 工具加载该技能的完整内容。\n")
        lines.append("")

        for skill in valid_skills:
            desc = skill.description or "无描述"
            if len(desc) > 120:
                desc = desc[:117] + "..."
            category_tag = f" [{skill.category}]" if skill.category else ""
            display = skill.display_name or skill.name
            lines.append(f"- **{display}**{category_tag}: {desc}")

        lines.append("")
        lines.append("> 提示：当需要使用某个技能时，调用 `load_skill` 工具加载完整内容后再执行。不要猜测技能的详细用法。")
        return "\n".join(lines)

    def _build_prompt_for_all(self) -> str:
        """构建全局技能提示词（向后兼容）。

        同 _build_prompt_for_skills：discipline 类别不进索引。
        """
        if not self.skills:
            return ""

        lines = ["## 可用技能索引\n"]
        lines.append("以下是你可用的技能列表。需要使用某个技能时，请先通过 load_skill 工具加载该技能的完整内容。\n")
        lines.append("")

        for name, skill in self.skills.items():
            if skill.category == 'discipline':
                continue  # 纪律不进索引，由 build_messages 工作模式触发时全文注入
            desc = skill.description or "无描述"
            if len(desc) > 120:
                desc = desc[:117] + "..."
            category_tag = f" [{skill.category}]" if skill.category else ""
            display = skill.display_name or name
            lines.append(f"- **{display}**{category_tag}: {desc}")

        lines.append("")
        lines.append("> 提示：当需要使用某个技能时，调用 `load_skill` 工具加载完整内容后再执行。不要猜测技能的详细用法。")
        return "\n".join(lines)

    # ====== 技能查询 ======

    def get_skill(self, name: str) -> Optional[Skill]:
        """获取技能"""
        return self.skills.get(name)

    def get_all_skills(self) -> List[Skill]:
        """获取所有技能（全局技能池）"""
        return list(self.skills.values())

    def get_skill_content(self, name: str, agent_id: str = None) -> str:
        """按需获取单个技能的完整内容

        如果传了 agent_id，会检查该角色是否装备了此技能
        未装备的技能也可以加载（灵活策略：知道就能用，但不会主动推荐）
        """
        skill = self.skills.get(name)
        if not skill:
            # 模糊匹配
            matches = [s for s in self.skills.values() if name.lower() in s.name.lower()]
            if len(matches) == 1:
                skill = matches[0]
            elif matches:
                available = ", ".join(m.name for m in matches)
                return f"找到多个匹配的技能: {available}。请指定更精确的名称。"
            else:
                available = ", ".join(self.skills.keys())
                return f"未找到技能 '{name}'。可用技能: {available}"

        return f"## 技能: {skill.name}\n\n{skill.content}"

    def get_skills_by_category(self) -> Dict[str, List[Skill]]:
        """按分类获取技能"""
        categories: Dict[str, List[Skill]] = {}
        for skill in self.skills.values():
            cat = skill.category or "未分类"
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(skill)
        return categories

    def get_recommended_skills(self, agent_id: str, limit: int = 5) -> List[Dict]:
        """根据角色模板推荐技能（基于标签匹配）

        未来可根据角色使用频率、任务类型等智能推荐
        现阶段基于角色的 template 字段做简单映射
        """
        # 读取角色模板信息
        meta_path = self.agents_dir / agent_id / "meta.json"
        template_name = ""
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding='utf-8-sig'))
                template_name = meta.get("template", "")
            except Exception:
                pass

        # 模板 → 推荐技能映射
        TEMPLATE_SKILL_MAP = {
            "liuruyan": ["frontend-design", "book-writer", "humanizer"],
            "chuyelan": ["context-compression", "session-compression"],
            "hanqingxue": ["codebuddy-code", "codebuddy-cli"],
            "fushanyue": ["self-improving-agent", "memory-manager"],
            "guwenshu": ["word-docx", "nano-pdf", "humanizer"],
            "shenweilan": ["chart-image", "context-compression", "excel-xlsx"],
            "xiawanling": ["frontend-design", "skill-creator"],
            "xiaohandu": ["self-improving-agent", "codebuddy-code"],
            "luchengfeng": ["codebuddy-code", "find-skills", "skill-creator"],
            "qingyuan": ["memory-manager", "self-improving-agent"],
        }

        recommended_names = TEMPLATE_SKILL_MAP.get(template_name, [])
        equipped = self.get_equipped_skills(agent_id)

        result = []
        for name in recommended_names:
            skill = self.skills.get(name)
            if skill and name not in equipped:
                result.append({
                    "name": skill.name,
                    "description": skill.description,
                    "category": skill.category,
                    "equipped": False,
                })
            if len(result) >= limit:
                break

        return result

    # ====== Discipline 取用 ======

    def get_equipped_disciplines(self, agent_id: str) -> List[Skill]:
        """返回角色装备的所有 discipline 类 skill 实体（按装备顺序）。

        使用方：agent.build_messages 在工作模式触发时调用，把这些 skill 的
        content 字段全文拼接注入 system_extra。

        和 get_equipped_skills 的区别：
        - get_equipped_skills 返回所有装备技能名（含 discipline）
        - 本方法只返回 discipline 类，且返回完整 Skill 对象（含 content）

        没装备任何 discipline 时返回空列表，调用方应走 fallback 路径。
        """
        equipped_names = self.get_equipped_skills(agent_id)
        result: List[Skill] = []
        for name in equipped_names:
            skill = self.skills.get(name)
            if skill and skill.category == 'discipline':
                result.append(skill)
        return result

    # ====== 缓存管理 ======

    def invalidate_equipped_cache(self, agent_id: str = None):
        """清除装备缓存
        
        agent_id=None 时清除所有缓存
        """
        if agent_id:
            self._equipped_cache.pop(agent_id, None)
        else:
            self._equipped_cache.clear()

    def reload(self):
        """重新加载所有技能 + pack sub-agents"""
        self.skills.clear()
        self.pack_agents.clear()
        self._equipped_cache.clear()
        self._load_all()


# 全局技能加载器
_skill_loader: Optional[SkillLoader] = None


def get_skill_loader(skills_dir: str = None) -> SkillLoader:
    """获取技能加载器单例"""
    global _skill_loader
    if _skill_loader is None:
        _skill_loader = SkillLoader(skills_dir)
    return _skill_loader


def get_skill_content_for_agent(name: str, agent_id: str = None) -> str:
    """Return shared skill content plus role-local mastery overlay when available."""
    loader = get_skill_loader()
    content = loader.get_skill_content(name, agent_id=agent_id)
    if not agent_id or content.startswith("未找到技能") or content.startswith("找到多个匹配的技能"):
        return content

    try:
        from .skill_growth import get_skill_growth_manager

        skill = loader.get_skill(name)
        skill_name = skill.name if skill else name
        overlay = get_skill_growth_manager(agent_id).get_prompt_overlay(skill_name)
        if overlay:
            return f"{content}\n\n{overlay}"
    except Exception as e:
        logger.warning("[SKILL] failed to build mastery overlay for %s/%s: %s", agent_id, name, e)

    return content


def _resolve_skill_for_overlay(loader: SkillLoader, name: str) -> Optional[Skill]:
    skill = loader.get_skill(name)
    if skill is not None:
        return skill

    lowered = (name or "").strip().lower()
    if not lowered:
        return None

    exact_match = next((item for item in loader.skills.values() if item.name.lower() == lowered), None)
    if exact_match is not None:
        return exact_match

    partial_matches = [item for item in loader.skills.values() if lowered in item.name.lower()]
    if len(partial_matches) == 1:
        return partial_matches[0]
    return None


def get_skill_content_for_agent(name: str, agent_id: str = None) -> str:
    """Return shared skill content plus role-local mastery overlay when available."""
    loader = get_skill_loader()
    skill = _resolve_skill_for_overlay(loader, name)
    canonical_name = skill.name if skill is not None else name
    content = loader.get_skill_content(canonical_name, agent_id=agent_id)
    if not agent_id or skill is None or not content.startswith("## "):
        return content

    try:
        from .skill_growth import get_skill_growth_manager

        overlay = get_skill_growth_manager(agent_id).get_prompt_overlay(skill.name)
        if overlay:
            return f"{content}\n\n{overlay}"
    except Exception as e:
        logger.warning("[SKILL] failed to build mastery overlay for %s/%s: %s", agent_id, skill.name, e)

    return content
