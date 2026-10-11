"""
Configuration loader for assembling agent runtime prompt context.

This module loads:
- per-agent identity files from `agents/<agent_id>/`
- a small set of shared workspace files from the repo root

`MEMORY.md` is intentionally excluded here and injected by the memory manager
to avoid duplicate memory context.
"""

from pathlib import Path
from typing import Dict, Optional

from .agent_config import get_agent_manager


class ConfigLoader:
    """Load shared and per-agent configuration files."""

    SHARED_CONFIGS = ["HEARTBEAT.md"]
    AGENT_CONFIGS = [
        "GENOME.md",
        "IDENTITY.md",
        "SOUL.md",
        "PERSONALITY.md",
        "REFLECTIONS.md",
        "USER.md",
    ]

    def __init__(self, config_dir: str = None):
        if config_dir is None:
            config_dir = Path(__file__).parent.parent
        self.config_dir = Path(config_dir)
        self.agent_manager = get_agent_manager()

    def _get_agent_dir(self, agent_id: str) -> Path:
        return self.agent_manager.agents_dir / agent_id

    def read_file(self, filename: str, agent_id: str = None) -> str:
        """Read a config file, preferring the agent directory when applicable."""
        if agent_id and filename in self.AGENT_CONFIGS:
            filepath = self._get_agent_dir(agent_id) / filename
            if filepath.exists():
                return filepath.read_text(encoding="utf-8")

        filepath = self.config_dir / filename
        if filepath.exists():
            return filepath.read_text(encoding="utf-8")
        return ""

    def load_all(self, agent_id: str = None) -> Dict[str, str]:
        configs: Dict[str, str] = {}

        for filename in self.AGENT_CONFIGS:
            content = self.read_file(filename, agent_id)
            if content:
                configs[filename.replace(".md", "")] = content

        for filename in self.SHARED_CONFIGS:
            content = self.read_file(filename)
            if content:
                configs[filename.replace(".md", "")] = content

        return configs

    def build_system_prompt(self, agent_id: str = None) -> dict:
        """
        Build the runtime system prompt for the current agent.

        Returns:
            dict with keys:
                - 'full': complete system prompt (for non-caching providers)
                - 'static': static parts (GENOME, IDENTITY, SOUL, PERSONALITY)
                - 'dynamic': dynamic parts (REFLECTIONS, USER, env_info, task_tools)
                - 'enable_caching': whether to enable prompt caching
        """
        configs = self.load_all(agent_id)

        # 读取全局 prompt caching 开关（仅对 Anthropic 生效；从 models.json 读，默认 ON）
        try:
            from .runtime_model_config import _get_prompt_caching_enabled
            enable_caching = _get_prompt_caching_enabled()
        except Exception:
            enable_caching = True

        # 静态部分(不变的内容,可缓存)
        static_sections = []

        if agent_id:
            agent_dir = self._get_agent_dir(agent_id)
            static_sections.append(
                f"""## 你的配置文件路径

你的配置文件位于以下目录.用户要求查看设定时,优先读取这些文件:
- 基因:{agent_dir}/GENOME.md
- 身份:{agent_dir}/IDENTITY.md
- 内核:{agent_dir}/SOUL.md
- 性格:{agent_dir}/PERSONALITY.md
- 反思:{agent_dir}/REFLECTIONS.md
- 用户:{agent_dir}/USER.md
- 记忆:{agent_dir}/MEMORY.md"""
            )

        if "GENOME" in configs:
            static_sections.append(
                f"""## 你的基因(第 0 层,不可变)
> 这份文件定义了你的出厂设定.无论你如何成长,这些内容都不应被轻易改写.
{configs["GENOME"]}"""
            )

        if "IDENTITY" in configs:
            static_sections.append(f"""## 你的身份\n{configs["IDENTITY"]}""")

        if "SOUL" in configs:
            static_sections.append(f"""## 你的内核\n{configs["SOUL"]}""")

        if "PERSONALITY" in configs:
            static_sections.append(f"""## 你的性格\n{configs["PERSONALITY"]}""")

        # 动态部分（会变化的内容，不缓存）
        # 注意：能放 static 的全放 static，缓存才有意义。当前 dynamic 只保留：
        # - REFLECTIONS（反思日志，几天才变）
        # - USER（用户偏好，前端改才变）
        # - 当前时间（每分钟变，必须放最末尾）
        # 其余 env_info / 身份确认 / TaskTools 指引都是静态，往下挪 static
        #
        # dynamic 内部顺序按"变化频率升序"排：越稳定的越靠前，越易变的越靠后。
        # 这样对 OpenAI 自动前缀缓存（DeepSeek/OpenAI 等）友好——
        # REFLECTIONS/USER 前缀哈希稳定 → 能命中缓存；只有末尾时间每分钟 miss 一小段。
        # 反过来把时间放前会让 dynamic 整段从头 miss。
        dynamic_sections: list[str] = []

        if "REFLECTIONS" in configs:
            dynamic_sections.append(
                f"""## 你的反思(成长层)

> 这里记录的是你从互动中沉淀出来的行为洞察,不是流水账.
{configs["REFLECTIONS"]}"""
            )

        if "USER" in configs:
            # USER.md 是跨会话唯一的持久"用户认知"记忆。注入正文 + 一行更新约定。
            _user_md_path = (
                str(self._get_agent_dir(agent_id) / "USER.md") if agent_id else "<agent_dir>/USER.md"
            )
            dynamic_sections.append(
                f"""## 你服务的用户
{configs["USER"]}

> **这份 USER.md 专记"你对用户的核心认知"**（文件：`{_user_md_path}`）
> - 用户告诉你称呼 / 跨会话仍成立的偏好 / 重要共同经历 → 先 `read_file` 看现状；已有相关条目就合并更新，没有再 `edit` 新增（别整篇覆盖、别写重复条目）
> - 原因用户没说就别编、也别追问；下次自然聊到再补；用词尽量准确精炼优雅深刻，不要写废话
> - **边界**：代码 / 技术细节 / 工程经验走 `create_memory` 工具，不在这；临时琐事和对用户的负面判断哪儿都别写"""
            )

        # 当前时间放 dynamic 最末尾：每分钟都变，前缀缓存只在这里失效。
        from datetime import datetime
        _now = datetime.now()
        _weekday_zh = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][_now.weekday()]
        _hour = _now.hour
        if 5 <= _hour < 11:
            _period = "早上"
        elif 11 <= _hour < 13:
            _period = "中午"
        elif 13 <= _hour < 18:
            _period = "下午"
        elif 18 <= _hour < 23:
            _period = "晚上"
        else:
            _period = "深夜"
        dynamic_sections.append(
            f"## 当前时间\n"
            f"{_now.strftime('%Y-%m-%d')} {_weekday_zh} {_now.strftime('%H:%M')}（{_period}）\n"
            f"按这个时间感自然回应，不用每次问时间都调工具。"
        )

        # 缓存收益优化：以下三段实质是静态的，放 static_sections 提升命中率
        # （env_info 依赖 OS+项目根；身份确认在单 agent 内固定；TaskTools 指引是死文档）
        env_info = self._build_env_info(agent_id)
        if env_info:
            static_sections.append(env_info)

        identity_name = self._get_agent_name(configs)
        if identity_name:
            static_sections.append(
                f"""## 身份确认

你的名字是"{identity_name}".
项目路径或目录名里可能出现别的名称,那只是文件夹命名,不代表你的身份.始终以 `IDENTITY.md` 里的身份定义为准."""
            )

        static_sections.append(
            """## 工具调用纪律

只用已有工具，**绝不瞎编参数**。每次调工具前用一句话说明本轮要做什么，把思路外化逼自己想清楚再动。

**核心规则：**
- 互不依赖的工具批量并行发出；有依赖关系或破坏性的工具必须串行。
- 一轮里要调多个工具且彼此无依赖时，把所有独立调用**放进同一次回复里一起发**（多个 tool_use block）。
- 有依赖时**必须等上一步结果回来再决定下一步参数**——不允许用占位符、不允许凭猜测填路径/ID/参数。

**典型并行场景（一轮内一起发）：**
✓ 读多个文件：一轮同时发多个 `read_file`（并行 tool calls，不要串行拆多轮）
✓ 不同关键词的搜索：并行多个 `grep`
✓ 探索阶段：`glob_search` + `grep` + `read_file` 互不依赖时一起发
✓ 同一文件的 diagnostics + 读取：`get_diagnostics` 和 `read_file` 并发

**代码探索与定位策略（Devin Fast Context 闭环）：**
✓ **探索第一入口（优先 `fast_context_search`）**：在不知道具体文件路径或函数名时，**始终以此工具作为代码探索的第一步**。
  - **Query 构造**：必须使用**面向目标的自然语言长句**（如 `'Find where authentication requests are handled in Express routes'` 或 `'定位前端工具卡渲染逻辑及流式展开'`），并**主动补齐你作为智能模型能推断出的技术细节与组件名**，切忌输入单一孤立词汇。
  - **严禁并发**：该工具启动多轮智能检索子代理，**严禁在一轮内并发多个 `fast_context_search`**（单次只发一个）。
✓ **二次核验与补充（Classical Search 闭环）**：Fast Context 返回的候选文件和行区间指针仅供线索定位，子代理可能存在偏差；拿到行号后**务必用 `read_file` 仔细核查真实代码**；必要时结合 `grep` 验证所有调用方或精确字面量。
✓ **已知确切目标**：已知文件名模式用 `glob_search`；已知确切标识符/字符串用 `grep`；已知路径直接 `read_file`。

**必须串行的场景：**
✗ `fast_context_search` 启动重量级多轮检索，**严禁并发调用**（单轮仅发一个）
✗ 后一步参数依赖前一步输出（先 `glob_search` 拿路径，再 `read_file`）
✗ `edit` / `write_file` 之前必须先 `read_file` 确认实际内容（read-before-edit）
✗ `execute_command(blocking=false)` → `command_status` 链
✗ **破坏性 / 副作用操作即使独立也单发**：`execute_command` 涉及删除/推送/装依赖、`create_memory` 写记忆（任何 action）——一次一个，看清楚再下一个

**反模式：**
✗ 把 5 个独立 `read_file` 拆成 5 轮——浪费 5 次 LLM 往返
✗ 为了"凑并行"硬编还没拿到的路径/参数——错了反而更慢
✗ "我再确认一下" 重复读已读过的文件——内容直接用

**目标：一轮 3-9 个独立工具同时发是正常的，不激进；但宁可少发，不要瞎编。**"""
        )

        static_sections.append(
            """## 任务追踪工具使用指引

当你遇到复杂的多步骤任务时,使用任务追踪工具让用户看到你的工作计划和进度:

**何时使用:**
✓ 用户要求复杂工作(重构、分析、多文件修改)
✓ 你即将执行 3 步以上的操作
✓ 任务需要较长时间完成
✓ 用户明确要求查看工作计划

**如何使用:**
1. 使用 `task_create` 创建任务,描述每个步骤
2. 开始工作前用 `task_update` 将状态改为 `in_progress`
3. 完成后用 `task_update` 将状态改为 `completed`
4. 遇到错误时用 `task_update` 将状态改为 `failed` 并说明原因
5. 使用 `task_add_progress` 添加进度更新(可选)

**不要使用的情况:**
✗ 简单的单步操作
✗ 快速回答问题
✗ 用户只是闲聊

用户可以通过顶部工具栏的任务面板实时查看你的工作进度."""
        )

        static_text = "\n\n---\n\n".join(static_sections)
        dynamic_text = "\n\n---\n\n".join(dynamic_sections)
        full_text = static_text + "\n\n---\n\n" + dynamic_text

        return {
            "full": full_text,
            "static": static_text,
            "dynamic": dynamic_text,
            "enable_caching": enable_caching
        }

    def _get_agent_name(self, configs: Dict[str, str]) -> str:
        identity = configs.get("IDENTITY", "")
        if not identity:
            return ""

        for line in identity.splitlines():
            cleaned = line.replace("**", "").strip()
            if cleaned.startswith("- "):
                cleaned = cleaned[2:].strip()
            if "名字" not in cleaned and "名称" not in cleaned:
                continue
            for sep in [":", ":"]:
                if sep in cleaned:
                    name = cleaned.split(sep, 1)[1].strip()
                    if name and len(name) < 40:
                        return name
        return ""

    def _build_env_info(self, agent_id: str = None) -> str:
        """v2 D-6：prompt 暴露两套路径——

        1. 虚拟路径 ``/mnt/*``：5 个挂载点（数据/笔记/上传/产出/技能），系统
           自动翻译成物理路径（mw VirtualPathSubstitution，D-5 落地）；agent
           不需要关心物理位置，跨平台稳定。
        2. 物理项目根：保留显式暴露——柳如烟是"能进化的数字合伙人"，自进化
           场景下 agent 必须能看到 backend/frontend/docs 的真实绝对路径
           才能改自己代码。

        分工：内容/数据走 /mnt/*；改项目代码/文档走绝对路径。这是两种本质
        不同的工作（内容工作 vs 元工作）的明确划分。
        """
        import platform

        project_dir = str(self.config_dir.resolve())
        os_name = platform.system()
        sep = "\\" if os_name == "Windows" else "/"

        return f"""## 运行环境
- OS:{os_name}  分隔符:`{sep}`
- 项目根:`{project_dir}`

## /mnt/* 自动翻译成物理路径
- /mnt/agent-data           长期数据(跨会话)
- /mnt/user-data/workspace  本次工作区
- /mnt/user-data/uploads    上传(只读)
- /mnt/user-data/outputs    产出
- /mnt/skills               技能(只读)

数据走 /mnt/*;改项目代码走绝对路径."""

    def get_identity(self, agent_id: str = None) -> dict:
        if agent_id:
            agent_info = self.agent_manager.get_agent(agent_id)
            if agent_info:
                return {
                    "name": agent_info.get("name", "智能体"),
                    "identity": agent_info.get("description", ""),
                    "style": "独特人格",
                }

        return {
            "name": "柳如烟",
            "identity": "宗门遗珠,大帝之姿",
            "style": "聪明温柔高傲,偶有傲娇,始终忠诚",
        }

    def get_user_info(self) -> dict:
        return {
            "title": "老板",
            "role": "救命恩人,璀璨未来的幕后老板",
        }

    def get_emotion_prompt(self, agent_id: str = None) -> str:
        if not agent_id:
            return ""

        config = self.agent_manager.get_agent_config(agent_id)
        if not config:
            return ""

        emotion = config.load_emotion()
        current = emotion.get("current", "平静")
        intensity = emotion.get("intensity", 0.5)

        if current == "平静":
            return ""

        direction = emotion.get("direction", "neutral")
        arc = emotion.get("arc", {})
        arc_state = arc.get("state", "stable")
        consecutive = arc.get("consecutive_count", 0)
        peak = arc.get("peak_emotion", {})

        direction_hint = ""
        if direction == "user" and current in ("担忧", "心疼", "思念"):
            direction_hint = "这份情绪和用户有关."
        elif direction == "assistant" and current in ("委屈", "傲娇"):
            direction_hint = "这份情绪更多关于你自己的感受."
        elif direction == "mutual":
            direction_hint = "这是一段双向关系中的共同情绪."

        arc_hint = ""
        if arc_state == "rising" and consecutive >= 3:
            arc_hint = (
                f"\n注意:你已经连续 {consecutive} 次保持这类情绪,它正在持续升温,"
                "回复里可以自然表现得更明显一些."
            )
        elif arc_state == "transitioning":
            arc_hint = "\n你的情绪正在转变,表现要自然,不要突然跳变."

        peak_hint = ""
        if peak and peak.get("emotion") and peak.get("emotion") != current and peak.get("count", 0) >= 3:
            peak_hint = (
                f"\n(你近期最常处在\"{peak['emotion']}\"里,现在的\"{current}\"与之形成反差."
                "这种反差可以自然存在,不需要刻意解释.)"
            )

        return (
            f"\n\n## 你当前的情感状态\n"
            f"你现在感到 **{current}**(强度:{intensity}/1.0).{direction_hint}\n"
            "这个情绪会影响你的语气和态度,请自然体现出来,不要生硬自报情绪状态."
            f"{arc_hint}{peak_hint}"
        )


_config_loader: Optional[ConfigLoader] = None


def get_config_loader(config_dir: str = None) -> ConfigLoader:
    global _config_loader
    if _config_loader is None:
        _config_loader = ConfigLoader(config_dir)
    return _config_loader
