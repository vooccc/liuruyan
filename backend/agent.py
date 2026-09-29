"""
智能体核心 - 整合配置、记忆、LLM，实现对话和记忆更新
支持多智能体实例，每个智能体拥有独立的配置和记忆

v2: 记忆系统改造
- 对话历史溢出检测和自动裁剪
- 记忆上下文预算控制
- get_context_for_chat() 不再无限制加载
"""
from typing import List, Dict, AsyncIterator, Optional, Any, Awaitable, Callable
import asyncio
import json
import logging
import os
import uuid
from datetime import datetime
import re

logger = logging.getLogger(__name__)

from .config import get_config_loader
from .memory_manager import (
    get_memory_manager,
    estimate_tokens,
    slim_history_tool_results,
    CONVERSATION_KEEP_RECENT_WARNING,
    CONVERSATION_KEEP_RECENT_OVERFLOW,
)
from .llm_client import get_llm_client, _active_call_config_var
from .providers.capabilities import infer_context_length
from .context_checkpoint import make_context_checkpoint
from .runtime_protocol import build_runtime_trace_event
from .skill_loader import get_skill_loader
from .agent_config import get_agent_manager
from .memory_refiner_v2 import MemoryRefinerV2
from .skill_growth import get_skill_growth_manager
from .llm_adapters import (
    history_entry_to_openai_std,
    to_anthropic_messages,
    to_openai_messages,
    infer_provider_kind,
    ProviderKind,
)


def _advance_history_index_by_non_system(
    history: List[Dict[str, Any]],
    start_index: int,
    non_system_count: int,
) -> int:
    """把压缩器的非 system 覆盖数映射回原始 transcript 历史索引。"""
    index = max(0, min(start_index, len(history)))
    remaining = max(0, int(non_system_count or 0))
    while index < len(history) and remaining > 0:
        if history[index].get("role") != "system":
            remaining -= 1
        index += 1
    return index


_PROMPT_SECTION_SEPARATOR = "\n\n---\n\n"


def _compose_system_prompt_with_checkpoint(
    prompt_data: Dict[str, Any],
    checkpoint_summary: Optional[str],
) -> str:
    """把稳定检查点放在 static 之后、dynamic 之前，保护最长缓存前缀。"""
    if not isinstance(checkpoint_summary, str) or not checkpoint_summary.strip():
        return str(prompt_data.get("full") or "")
    parts = [
        str(prompt_data.get("static") or "").strip(),
        checkpoint_summary.strip(),
        str(prompt_data.get("dynamic") or "").strip(),
    ]
    return _PROMPT_SECTION_SEPARATOR.join(part for part in parts if part)


# ════════════════════════════════════════════════════════════════════════
# 工作模式检测：把"做事档位"的纪律从 SOUL 里的软规则，
# 升级成由代码触发的硬注入。
#
# 设计动机：
# SOUL.md 写的纪律是 always-on 但软强度，模型在长对话里会自然稀释。
# 这里通过"信号触发 → 追加 system_extra 块"把纪律重新强化到当前轮的
# 显著位置（system 末尾，离 user 消息最近 → 注意力权重更高）。
#
# 信号分两档：
#   强信号（必触发）：最近轮有 tool_call、本会话有 in_progress 任务
#   弱信号（关键词）：消息含工作动词 + 技术名词；情感词强时反而不触发
# ════════════════════════════════════════════════════════════════════════

# 工作动词：用户表达"想让 agent 干技术活"的动作词
_WORK_VERB_PATTERNS = [
    # 中文：典型委派句式
    r'帮我\s*(写|改|修|做|实现|查|看|分析|读|跑|测|调|排查|定位|优化|重构|部署|review|debug|fix|审|加|删|加上|去掉)',
    r'(写|改|修|实现|查|看|分析|读|跑|测|调|排查|定位|优化|重构|部署)\s*(下|个|一下|代码|脚本|函数|接口|文件|bug|错误|问题|报错|异常)',
    # 命令式句首
    r'^(写|改|修|实现|查|看|分析|读|跑|测|调|排查|定位|优化|重构|部署)',
    # 英文动词
    r'\b(implement|fix|debug|refactor|analyze|optimize|deploy|review|investigate|build)\b',
]

# 技术名词/格式：消息里含"明显的代码/技术内容"
_WORK_TECH_PATTERNS = [
    r'\b(bug|error|exception|traceback|stack|log|warning)\b',
    r'\.(py|ts|tsx|js|jsx|md|json|sh|css|html|yml|yaml|toml|sql|go|rs|java|cpp|h)(?!\w)',
    r'```',                        # 三反引号代码块
    r'`[^`\n]{3,}`',               # 行内反引号（>=3 字符避免误判中文标点）
    r'报错|报警|出错|异常|错误信息|栈|崩了|挂了|卡住|不工作',
    r'(/[A-Za-z0-9_.-]+){2,}',     # 路径形 /a/b/c
    r'[A-Za-z_][A-Za-z0-9_]*\([^)]*\)',  # 形如 func() 的调用
]

# 情感词：检测到强情感诉求时不进入工作模式（保持陪伴档位）
# 只有当工作信号同时也很强时（动词 + 技术词都齐）才覆盖
_EMOTIONAL_PATTERNS = [
    r'(心情|难受|开心|沮丧|郁闷|烦|累|想你|陪我|聊聊|抱抱|安慰|寂寞|心累)',
    r'(怎么办呀|怎么办啊|哎|嗯嗯|嘤嘤|呜呜)',
]


def _has_recent_tool_call(history: List[Dict], lookback: int = 4) -> bool:
    """扫描最近 N 条消息，判断是否存在 tool_use / tool_calls。

    兼容两种 provider 形态：
    - Anthropic：assistant.content 是 list[dict]，含 {type: 'tool_use'}
    - OpenAI std：assistant.tool_calls 顶层数组
    """
    if not history:
        return False
    for entry in reversed(history[-lookback:]):
        if entry.get('role') != 'assistant':
            continue
        # OpenAI std
        if entry.get('tool_calls'):
            return True
        # Anthropic content blocks
        content = entry.get('content')
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get('type') == 'tool_use':
                    return True
    return False


def _has_active_tasks(session_id: Optional[str]) -> int:
    """返回当前 session 内 in_progress 任务数（0 表示无）。
    任务工具不可用时返回 0，不影响主流程。"""
    if not session_id:
        return 0
    try:
        from .tools.builtin.task_tools import get_task_manager
        active = get_task_manager().list_tasks(
            status='in_progress',
            session_id=session_id,
        )
        return len(active)
    except Exception:
        return 0


def detect_work_mode(
    user_message: str,
    conversation_history: Optional[List[Dict]],
    session_id: Optional[str],
) -> tuple[bool, str]:
    """判断当前轮次是否进入"工作模式"。

    Returns:
        (is_work, reason): 是否激活 + 触发理由（'+' 拼接多个原因，便于日志诊断）
    """
    msg = (user_message or '').strip()
    reasons: List[str] = []

    # ── 强信号 ──
    if _has_recent_tool_call(conversation_history or []):
        reasons.append('recent_tool_call')
    n_active = _has_active_tasks(session_id)
    if n_active > 0:
        reasons.append(f'active_tasks={n_active}')

    # ── 弱信号（关键词）──
    has_verb = any(re.search(p, msg, re.IGNORECASE) for p in _WORK_VERB_PATTERNS)
    has_tech = any(re.search(p, msg, re.IGNORECASE) for p in _WORK_TECH_PATTERNS)
    has_emo = any(re.search(p, msg) for p in _EMOTIONAL_PATTERNS)

    # 反信号：情感优先——除非工作动词 + 技术词都明确才覆盖
    if has_emo and not (has_verb and has_tech) and not reasons:
        return False, 'emotional_priority'

    if has_verb:
        reasons.append('verb')
    if has_tech:
        reasons.append('tech')

    return (len(reasons) > 0), '+'.join(reasons) if reasons else 'none'


def build_fallback_work_hint(reason: str, available_tools: Optional[set] = None) -> str:
    """工作模式纪律的硬编码 fallback（无装备 discipline skill 时用）。

    available_tools: 该 agent 生效白名单（None = 全开 / 白名单读取失败时降级）。
    hint 引用的工具名按白名单过滤——提示词不能指挥模型调它没有的工具
    （极简档没有 multi_edit，自定义白名单可能连 grep 都没有）；
    某段全部是工具引用而工具都缺席时整段跳过，编号动态重排。
    """
    def has(*names: str) -> bool:
        return available_tools is None or any(n in available_tools for n in names)

    locate_lines: List[str] = []
    if has('read_file'):
        locate_lines.append('   - 改任何代码前先 read_file 看完整上下文，禁止凭记忆改')
    locate_pair: List[str] = []
    if has('fast_context_search'):
        locate_pair.append('定位未知代码/概念位置优先用 fast_context_search')
    if has('grep'):
        locate_pair.append('已知确切标识符或验证假设用 grep')
    if locate_pair:
        locate_lines.append('   - ' + '；'.join(locate_pair))
    if locate_lines:
        locate_lines.append('   - 不确定时 → 直接问用户，不要硬猜')

    call_lines: List[str] = []
    if has('fast_context_search'):
        call_lines.append('   - 独立的 read/grep 一次发多个，但 fast_context_search 严禁并发（单次只调一个）')
    if has('multi_edit'):
        call_lines.append('   - 批量改同一文件用 multi_edit')
    call_lines.append('   - 引用代码必须带绝对路径行号：@/path/to/file.py:123-130')

    sections: List[str] = []
    if locate_lines:
        sections.append('先定位再动手\n' + '\n'.join(locate_lines))
    sections.append(
        '最小修复原则\n'
        '   - 一个 bug 一行能解决就一行\n'
        '   - 不顺手重构、不顺手加注释、不主动加 emoji\n'
        '   - bug 修复和重构永远分开提交'
    )
    sections.append(
        '不发半成品\n'
        '   - 改完自检（AST/语法/最简测试）才能说"完成"\n'
        '   - 不在 except 里 pass 吞错——这是给未来挖坟\n'
        '   - 没全做完就说没全做完，不要含糊'
    )
    sections.append(
        '上游优先\n'
        '   - 同一 bug 上下游都能改时，改源头\n'
        '   - 下游 workaround 必须留 # WORKAROUND 注释说明真正问题'
    )
    if call_lines:
        sections.append('工具调用纪律\n' + '\n'.join(call_lines))

    body = '\n'.join(f'{i}. {section}' for i, section in enumerate(sections, start=1))
    return (
        '\n\n[工作模式启动]\n'
        f'信号：{reason}。下面是做事时不可妥协的纪律——不是建议，是底线。\n'
        '\n'
        f'{body}\n'
        '\n'
        '切回对话档位的信号：用户停止技术请求、转向情感/闲聊。'
        '工作模式与陪伴模式不冲突——干活时该温柔的语气依旧温柔，'
        '但纪律不让步。'
    )


def compute_context_breakdown(
    base_system: str,
    system_extra: str,
    messages: List[Dict],
    agent_id: Optional[str] = None,
    model_id: Optional[str] = None,
    context_length: Optional[int] = None,
) -> Dict[str, Any]:
    """估算本轮请求的上下文构成（分段 token），给前端徽章的展开面板。

    分段口径（与注入路径一一对应）：
    - system: 主人格提示词（GENOME/IDENTITY/SOUL/... + checkpoint）
    - extra: 动态注入（工作模式 hint / cron hint / pinned / PROJECT_NOTES 等）
    - messages: 消息历史（含本条 user 消息和 tool 消息）
    - tools: 该角色生效白名单的工具 description+schema 总和（与 LLM 实际注入一致，
      档位/白名单经 get_allowed_tools 展开，未配置 = 全部工具）

    context_length 走 Agent.get_active_context_length()（active call config 的
    ModelEntry.context_length 显式值，或 capabilities 能力表按模型名推断）。
    全部 token 走 backend.token_estimation 校准估算器；只做 UI 展示，不参与业务
    决策。任何一段估算失败都静默归零，绝不阻断 chat 主流程。
    """
    from backend.token_estimation import approx_tokens, message_tokens, tool_token_estimate

    try:
        system_tokens = approx_tokens(base_system or "")
    except Exception:
        system_tokens = 0
    try:
        extra_tokens = approx_tokens(system_extra or "")
    except Exception:
        extra_tokens = 0
    try:
        messages_tokens = sum(message_tokens(m) for m in (messages or []))
    except Exception:
        messages_tokens = 0

    tools_tokens = 0
    try:
        from backend.agent_config import get_agent_manager
        from backend.tools.builtin.clean_registry import registry
        allowed = get_agent_manager().get_allowed_tools(agent_id) if agent_id else None
        if allowed is None:
            names = list(registry.tools.keys())
        else:
            names = [n for n in allowed if n in registry.tools]
        for n in names:
            t = registry.tools.get(n) or {}
            tools_tokens += tool_token_estimate(
                t.get("description") or "", t.get("input_schema") or {}
            )
    except Exception:
        tools_tokens = 0

    # 压缩触发线与 memory_manager.resolve_conversation_thresholds 口径一致（含安全上限）
    if context_length:
        from .memory_manager import resolve_conversation_thresholds
        _, compact_threshold = resolve_conversation_thresholds(context_length)
    else:
        compact_threshold = None

    return {
        "system": system_tokens,
        "extra": extra_tokens,
        "messages": messages_tokens,
        "tools": tools_tokens,
        "total": system_tokens + extra_tokens + messages_tokens + tools_tokens,
        "model": model_id,
        "context_length": context_length,
        "compact_threshold": compact_threshold,
    }


class Agent:
    """智能体（支持多实例 + 多会话）

    v3 改造要点:
    1. conversation_history 不再由 Agent 维护，每次调用由调用方传入
    2. chat() 支持传入 session_id，自动从 SessionManager 获取跨会话感知
    3. Agent 是无状态的 LLM 调用器，对话历史/情感/面具全部由 Session 管理
    """

    def __init__(self, agent_id: str = None, config_dir: str = None):
        """初始化智能体

        Args:
            agent_id: 智能体 ID，为 None 时使用 default
            config_dir: 全局配置目录（兼容旧版）
        """
        self.agent_id = agent_id or "default"
        self.config_loader = get_config_loader(config_dir)
        self.memory_manager = get_memory_manager(agent_id=self.agent_id)
        self.llm_client = get_llm_client()
        self.skill_loader = get_skill_loader()

        # 记忆提炼器（延迟初始化，避免未使用时创建）
        self._refiner: Optional[MemoryRefinerV2] = None
        self._refine_cooldown = 0  # 冷却计数器，每轮-1

        # 对话历史不再由 Agent 维护，保留仅为向后兼容
        # 调用方应通过 build_messages() 传入 history
        self.conversation_history: List[Dict[str, str]] = []

        # 系统提示（缓存）
        self._system_prompt: Optional[str] = None

        # v2 chain shim — lazy 创建（首次 chat 时）
        # 受 LIURUYAN_USE_V2_CHAIN 控制，默认 ON
        self._v2_lead: Optional[Any] = None

    @staticmethod
    def _should_use_v2_chain() -> bool:
        """v2 chain feature flag。LIURUYAN_USE_V2_CHAIN=0 时退回 v1，否则走 chain。"""
        return os.environ.get("LIURUYAN_USE_V2_CHAIN") != "0"

    def _build_run_context(self, *, session_id: Optional[str]) -> Any:
        """构造 v2 RunContext（每次 chat 新建，含新 run_id）。"""
        # 必须用短路径 liuruyan.*——长路径 backend.packages.harness.liuruyan.*
        # 会让同一文件加载成两个独立 module（ContextVar 不通）。
        # 详见 MEMORY feedback_harness_import_short_path_only.md
        from liuruyan.middlewares import RunContext
        return RunContext(
            user_id="local",  # 柳如烟单机无登录概念，固定值
            agent_id=self.agent_id,
            session_id=session_id or "",
            run_id=str(uuid.uuid4()),
            trace_id=None,
            channel="chat",
            request_id=None,
        )

    def _ensure_v2_lead(self) -> Any:
        """lazy 实例化 LeadAgent；chain list 不可变，per-Agent 缓存。

        C5-c-3：构造 summarizer DI 注入 LeadAgent；mw 09 在 flag
        ``LIURUYAN_SUMMARIZE_ACTIVATE=1`` 时调它做真摘要。
        """
        if self._v2_lead is None:
            # 短路径 import — 避免双 module 隐患（详见 MEMORY
            # feedback_harness_import_short_path_only.md）
            from liuruyan.agents import LeadAgent
            try:
                from backend.v2_summarizer import make_summarizer
                summarizer = make_summarizer(self.llm_client)
            except Exception:
                # summarizer 构造失败不阻断 LeadAgent；mw 09 退回 plan-only
                import logging as _logging
                _logging.getLogger(__name__).exception(
                    "[Agent._ensure_v2_lead] make_summarizer failed; "
                    "mw 09 will fall back to plan-only mode"
                )
                summarizer = None
            self._v2_lead = LeadAgent(self, summarizer=summarizer)
        return self._v2_lead

    def get_system_prompt(self, session_id: str = None) -> dict:
        """获取系统提示（带记忆上下文、技能和情感）

        v2: 不再在此处加载 MEMORY.md，避免重复注入
        MEMORY.md 由 memory_manager.get_context_for_chat() 统一管理

        v3: 新增 session_id 参数，用于注入跨会话感知

        v4: 注入社交关系信息，让角色知道其他角色的存在

        v5: 支持 Prompt Caching，返回 dict 格式
            - full: 完整提示词（用于非缓存模式）
            - static: 静态部分（可缓存）
            - dynamic: 动态部分（不缓存）
            - enable_caching: 是否启用缓存

        Returns:
            dict with keys: full, static, dynamic, enable_caching
        """
        prompt_data = self.config_loader.build_system_prompt(self.agent_id)

        # 注入 .lry/rules/ + workflow index 到 static prefix（保证 prompt cache 命中）。
        # 拼到 static 末尾、dynamic 之前——这样：
        # 1) AnthropicCachePlugin 仍能 startswith-match 整个扩展后的 static
        # 2) 同一项目下规则 / workflow 列表稳定时 cache 完整命中
        # 3) 切换 working_directory 到不同项目时 cache 自然 miss（预期行为）
        # workflow 只注入索引（name + description），完整步骤靠 get_project_workflow 按需取
        # 失败一律静默降级：.lry/ 是增强能力，不能阻塞主对话。
        # 详见 _docs/lry_workspace_design.md §7.2
        try:
            from .lry_workspace import (
                load_codemap_pointer,
                load_project_rules,
                load_workflow_index,
                resolve_workspace_root,
            )

            _wd_root = resolve_workspace_root()
            if _wd_root is not None:
                _project_static_parts: list[str] = []
                _rules_text = load_project_rules(_wd_root)
                if _rules_text:
                    _project_static_parts.append(_rules_text)
                _wf_index = load_workflow_index(_wd_root)
                if _wf_index:
                    _project_static_parts.append(_wf_index)
                _codemap_text = load_codemap_pointer(_wd_root)
                if _codemap_text:
                    _project_static_parts.append(_codemap_text)

                if _project_static_parts:
                    _project_static_text = "\n\n---\n\n".join(_project_static_parts)
                    prompt_data["static"] = (
                        prompt_data["static"]
                        + "\n\n---\n\n"
                        + _project_static_text
                    )
                    prompt_data["full"] = (
                        prompt_data["static"]
                        + "\n\n---\n\n"
                        + prompt_data["dynamic"]
                    )
        except Exception as _exc:
            logger.warning("[LRY_WORKSPACE] load_project_static failed: %s", _exc)

        base_prompt = prompt_data["full"]
        enable_caching = prompt_data["enable_caching"]

        # 添加社交关系信息（让角色知道自己认识谁）
        try:
            from .social_graph import get_social_graph
            from . import paths
            sg = get_social_graph(str(paths.memory_dir()))
            # 确保已初始化
            if not sg._agent_ids:
                from .agent_config import get_agent_manager
                manager = get_agent_manager()
                agent_ids = [a["id"] for a in manager.list_agents()]
                for tmpl in manager.list_templates():
                    tid = tmpl.get("template_id", "")
                    if tid and tid not in agent_ids:
                        agent_ids.append(tid)
                sg.initialize(agent_ids)
            relation_prompt = sg.build_relation_prompt(self.agent_id)
            if relation_prompt:
                base_prompt += "\n\n" + relation_prompt
        except Exception as e:
            import traceback
            traceback.print_exc()
            pass  # 社交图谱不是必需的

        # 工具路由提示：根据 MCP server 当前 running 状态动态注入。
        # 关掉某 server，对应 hint 自动消失；不改角色 .md 任何文件，零污染。
        # 放 dynamic 末尾（跟 emotion / mask / skills 同级），不进 prompt cache 静态段。
        try:
            from .mcp.controller import get_controller
            from .mcp.types import MCPServerState
            ctrl = get_controller()
            running = {
                sid for sid in ctrl.list_servers()
                if ctrl.get_status(sid).state == MCPServerState.RUNNING
            }
            hints: list[str] = []
            if "codegraph" in running:
                hints.append(
                    "**CodeGraph MCP**：调用链 / callers / callees / impact / trace / "
                    "跨文件符号上下文 / 项目结构索引。涉及代码理解、重构、影响分析、"
                    "找调用关系类问题，**优先用 `mcp__codegraph__codegraph_*`**——"
                    "比 grep / read_file 一行行翻快得多，比 code_intelligence 看得更结构化。"
                    "只有 CodeGraph 结果不够时再用 code_intelligence / grep 补充。"
                )
            if hints:
                routing_block = (
                    "\n\n## 代码分析工具路由\n\n"
                    + "\n\n".join(f"- {h}" for h in hints)
                    + "\n\n（这些工具关闭时此清单会自动消失，不必特别声明。）"
                )
                base_prompt += routing_block
        except Exception:
            pass  # 工具路由提示不是必需的

        # 添加情感上下文
        emotion_prompt = self.config_loader.get_emotion_prompt(self.agent_id)
        if emotion_prompt:
            base_prompt += emotion_prompt

        # 添加面具状态提示（仅对有mask系统的智能体生效）
        try:
            from .mask_system import get_mask_system
            mask_sys = get_mask_system()
            mask_prompt = mask_sys.get_mask_prompt(self.agent_id)
            if mask_prompt:
                base_prompt += mask_prompt
        except Exception:
            pass  # 面具系统不是必需的

        # 添加技能提示（基于角色装备槽，无装备则注入全局技能池）
        skills_prompt = self.skill_loader.get_skills_prompt(agent_id=self.agent_id)
        if skills_prompt:
            base_prompt += "\n\n" + skills_prompt

        # 添加跨会话感知提示（Phase 1）
        if session_id:
            try:
                from .session import get_session_manager
                import asyncio
                session_mgr = get_session_manager()
                # 尝试获取同步版本的感知信息（SessionManager 内存中直接可读）
                profile = session_mgr._profiles.get(self.agent_id)
                if profile:
                    peers = profile.get_peer_awareness(session_id)
                    if peers:
                        peer_lines = []
                        for p in peers:
                            status_text = "活跃中" if p["status"] == "active" else "空闲"
                            emotion_text = p.get("emotion_label", "")
                            peer_lines.append(f'- "{p["title"]}" — {status_text}，情感：{emotion_text}')
                        awareness_text = "\n".join(peer_lines)
                        base_prompt += f"\n\n## 跨会话感知\n\n【当前你还有其他对话正在进行】\n{awareness_text}\n\n你可以感知这些对话的存在，但不应提及它们的具体内容。"
            except Exception:
                pass  # 跨会话感知不是必需的

        # 注入走廊收件箱消息（Phase 3 走廊模式）
        try:
            from .social_bus import get_message_bus
            bus = get_message_bus()
            inbox_prompt = bus.get_inbox_prompt(self.agent_id)
            if inbox_prompt:
                base_prompt += f"\n\n{inbox_prompt}"
        except Exception:
            pass  # 消息总线不是必需的

        return {
            "full": base_prompt,
            "static": prompt_data["static"],
            "dynamic": prompt_data["dynamic"],
            "enable_caching": enable_caching
        }

    def get_memory_context(self, budget_tokens: int = 8000) -> str:
        """获取记忆上下文（带预算控制）

        Args:
            budget_tokens: 记忆上下文 token 预算
        """
        self.memory_manager.prefetch(budget_tokens=budget_tokens)
        return self.memory_manager.get_context_for_chat(budget_tokens=budget_tokens)

    def get_active_context_length(self) -> Optional[int]:
        """返回本次调用实际模型的上下文窗口，供 v1/v2 压缩链共同使用。"""
        call_config = _active_call_config_var.get()
        if call_config is not None:
            if call_config.context_length:
                return call_config.context_length
            if call_config.model:
                return infer_context_length(call_config.model)
        return infer_context_length(getattr(self.llm_client, "model", "") or "")


    async def build_messages(
        self,
        user_message: str,
        session_id: str = None,
        conversation_history: List[Dict] = None,
        system_extra: str = None,
        recall_context: str = None,
        provider_kind: ProviderKind | None = None,
        user_message_already_in_history: bool = False,
        compression_progress: Optional[
            Callable[[Dict[str, Any]], Awaitable[None]]
        ] = None,
    ) -> tuple[list[dict], dict]:
        """构建消息列表（按 provider_kind 走对应 adapter 归一化）。

        Phase B/C/D/E 改造历程：
        - 老 build_messages（已删）扁平化 assistant content 为第一段 text，
          丢光所有 tool_use/tool_result blocks，导致跨轮 tool 记忆消失
        - Phase B/C 引入 build_messages_v2（flag 控制），双轨并行真机验证
        - Phase D 真机稳定后删 v1，v2 改名回 build_messages 成默认
        - Phase E (2026-05-20) 盘上 v1/v2 残留清零 → 删 normalize 内部 v1/v2
          分支 + 改名为 history_entry_to_openai_std

        归一化逻辑（v3 单格式）：
        1. 历史消息经过 history_entry_to_openai_std：v3 Anthropic content blocks
           拆成 OpenAI std 序列（assistant + 配套 tool messages）
        2. 按 provider_kind 选 adapter（anthropic → list[ContentBlock]，
           openai_compat → OpenAI std 扁平 string content）
        3. tool_calls 永远是顶层数组（不再嵌进 segments string）
        4. 盘上出现 v1/v2 老格式 → transcript.rebuild_conversation_history
           fail-fast，不再源头兑底

        Args:
            user_message: 用户消息
            session_id: 会话 ID
            conversation_history: 对话历史
            system_extra: 额外的 system 内容
            recall_context: 临时回忆上下文
            provider_kind: 推断 provider 类型（anthropic / openai_compat / unknown）
                为 None 时自动从配置推断
            compression_progress: 可选的异步进度回调；仅在真实触发上下文压缩时调用

        Returns:
            tuple[list[dict], dict]: (messages, prompt_cache_info)
        """

        prompt_data = self.get_system_prompt(session_id=session_id)
        base_system = prompt_data["full"]

        # cron 三件套 + sleep（FEATURE_CRON_TOOL 门控）
        _cron_env = os.getenv("FEATURE_CRON_TOOL", "")
        _cron_on = (_cron_env or "").strip().lower() in ("1", "true", "yes", "on")
        # 启动期诊断：第一次 build_messages 时输出一次，后续别刷屏
        if not getattr(self, "_cron_hint_logged", False):
            logger.debug("[CRON_HINT] FEATURE_CRON_TOOL=%r inject=%s", _cron_env, _cron_on)
            self._cron_hint_logged = True
        if _cron_on:
            cron_hint = (
                "\n\n[cron_create / cron_list / cron_delete / cron_set_enabled / sleep]"
                " 内置定时工具（非 OS cron/schtasks）。\n"
                "『每天/每周/定时/早 X 点』→ cron_create(recurring)；"
                "『明天/下周/一次性/提醒我』→ cron_create(once)。\n"
                "shell 措辞（echo/ls）也调它，prompt 转自然语言。"
                "想周期/轮询永远 cron_create，不要 sleep 等。\n"
                "查/删/停 → cron_list / cron_delete / cron_set_enabled。"
            )
            system_extra = (system_extra or "") + cron_hint

        # 工作模式检测 → 硬注入纪律（FEATURE_WORK_MODE_HINT 门控，默认 ON）
        # 设计：SOUL.md 里的纪律是 always-on 软规则；这里在检测到"做事场景"时
        # 把纪律以更紧迫、更具体的形式重新塞进 system 末尾，强化注意力权重。
        # 启动期日志：第一次 build_messages 输出 hint 启用状态。
        _wmh_env = os.getenv("FEATURE_WORK_MODE_HINT", "1")
        _wmh_on = (_wmh_env or "").strip().lower() in ("1", "true", "yes", "on")
        if not getattr(self, "_wmh_logged", False):
            logger.debug("[WORK_MODE] FEATURE_WORK_MODE_HINT=%r inject=%s", _wmh_env, _wmh_on)
            self._wmh_logged = True
        if _wmh_on:
            is_work, reason = detect_work_mode(
                user_message=user_message,
                conversation_history=conversation_history,
                session_id=session_id,
            )
            if is_work:
                # 优先：用装备的 discipline skill 全文拼接
                # 兜底：装备列表为空时，用硬编码 5 条作 fallback（保证任何角色
                # 进入工作模式都至少有纪律托底，不会裸奔）
                disciplines = self.skill_loader.get_equipped_disciplines(self.agent_id)
                if disciplines:
                    names = ', '.join(d.name for d in disciplines)
                    logger.info(
                        "[WORK_MODE] activated session=%s reason=%s disciplines=[%s]",
                        session_id, reason, names,
                    )
                    body = "\n\n---\n\n".join(d.content.strip() for d in disciplines)
                    work_hint = (
                        "\n\n[工作模式启动]\n"
                        f"信号：{reason}。已装备纪律：{names}。\n"
                        "下面是做事时不可妥协的纪律——不是建议，是底线。\n\n"
                        f"{body}"
                    )
                else:
                    logger.warning(
                        "[WORK_MODE] activated session=%s reason=%s disciplines=<fallback hardcoded>",
                        session_id, reason,
                    )
                    # 白名单感知：hint 引用的工具名按该 agent 实际生效配置过滤
                    # （tool_preset 档位 / tools 白名单），避免指挥模型调它没有的工具
                    try:
                        from backend.agent_config import get_agent_manager
                        _allowed = get_agent_manager().get_allowed_tools(self.agent_id)
                    except Exception:
                        _allowed = None  # 读取失败降级为全开视图，hint 全量渲染
                    _avail = {str(t) for t in _allowed} if _allowed is not None else None
                    work_hint = build_fallback_work_hint(reason, _avail)
                system_extra = (system_extra or "") + work_hint

        # Pinned context 上下文图钉注入与指导原则机制
        # session 上有用户钉的 guideline + scope items 时，渲染成文本块挂到 system_extra
        # 末尾——挂在末尾、离 user 消息最近，注意力权重最高，跟 work_hint 一个套路。
        # session_id 缺失或 session 找不到时静默跳过；解析单条失败由 scope_item 内部兜底。
        if session_id:
            try:
                from .session import get_session_manager
                from .scope_item import render_pinned_scope

                _sess = await get_session_manager().get_session(session_id)
            except Exception:
                _sess = None
            if _sess is not None:
                pinned_blocks: list[str] = []
                _guideline = (getattr(_sess, "pinned_guideline", None) or "").strip()
                if _guideline:
                    pinned_blocks.append(f"[Pinned Guideline]\n{_guideline}")
                try:
                    _scope_items = _sess.get_pinned_scope_items()
                except Exception:
                    _scope_items = []
                if _scope_items:
                    rendered = render_pinned_scope(_scope_items)
                    if rendered.strip():
                        pinned_blocks.append(f"[Pinned Context]\n{rendered}")
                if pinned_blocks:
                    system_extra = (system_extra or "") + "\n\n" + "\n\n".join(pinned_blocks)

        # 注入 .lry/PROJECT_NOTES.md 到 system_extra（动态段，不缓存）。
        # 放在 system_extra 末尾、紧贴 user message 之前，注意力权重最高，
        # 跟 work_hint / pinned scope 同级。失败静默降级。
        try:
            from .lry_workspace import resolve_workspace_root, load_project_notes

            _wd_root = resolve_workspace_root()
            if _wd_root is not None:
                _notes_text = load_project_notes(_wd_root)
                if _notes_text:
                    system_extra = (system_extra or "") + "\n\n" + _notes_text
        except Exception as _exc:
            logger.warning("[LRY_WORKSPACE] load_project_notes failed: %s", _exc)

        # 在拼装主 system 之前加载检查点。稳定顺序固定为：
        # static -> checkpoint -> dynamic -> system_extra。
        # 这样新增一轮消息只会改变尾部；已有 static/checkpoint 前缀仍可命中缓存。
        raw_history = list(
            conversation_history
            if conversation_history is not None
            else self.conversation_history
        )
        checkpoint = None
        checkpoint_manager = None
        covered_message_count = 0
        if session_id and raw_history:
            try:
                from .session import get_session_manager

                checkpoint_manager = get_session_manager()
                checkpoint = await checkpoint_manager.load_context_checkpoint(
                    session_id,
                    raw_history,
                )
            except Exception:
                logger.warning(
                    "[CONTEXT_CHECKPOINT] load failed session=%s",
                    session_id,
                    exc_info=True,
                )
                checkpoint = None

        active_checkpoint_summary = (
            str((checkpoint or {}).get("summary") or "").strip() or None
        )
        if checkpoint is not None:
            covered_message_count = int(
                checkpoint.get("covered_message_count") or 0
            )

        base_system = _compose_system_prompt_with_checkpoint(
            prompt_data,
            active_checkpoint_summary,
        )
        if system_extra:
            system_content = f"{base_system}\n\n{system_extra}"
        else:
            system_content = base_system

        messages: list[dict] = []
        primary_system_message = None
        if system_content:
            primary_system_message = {"role": "system", "content": system_content}
            messages.append(primary_system_message)

        # 记忆上下文
        # v2 C3-a：chain ON 时由 mw 03 UserMemoryInject 接管注入（避免重复）；
        # chain OFF 时仍走 v1 主路径。chain shim 内部 LeadAgent._build_state
        # 调 self.agent.get_memory_context() 拿同样的字符串塞 state["memory_text"]，
        # mw 03 在 before_model 钩子注入到第一条 user 之前 — 行为跟 v1 等价。
        if not self._should_use_v2_chain():
            memory_context = self.get_memory_context()
            if memory_context:
                messages.append({
                    "role": "system",
                    "content": f"以下是你的记忆上下文：\n\n{memory_context}"
                })

        # 临时回忆上下文
        if recall_context:
            messages.append({
                "role": "system",
                "content": f"以下是你本轮临时回忆到的历史片段，仅用于回答这一次回忆请求：\n\n{recall_context}"
            })

        # 对话历史：transcript 原文与模型工作上下文分轨。
        # checkpoint 只替代已覆盖前缀进入模型，不修改 raw_history / transcript。
        history_tail = raw_history[covered_message_count:]

        # 历史工具结果瘦身（2026-06-09 fix①）—— fix②（同日）把它从 overflow 检测
        # 之后提到了**之前**。原因：溢出判定/压缩若看的是未瘦身的原始 history，
        # 单条 7.8 万 token 的 tool_result 就能误触发 overflow、白调一次昂贵 LLM 摘要，
        # 而其实 slim 一刀（每条旧 tool_result 截到 HISTORY_TOOL_RESULT_MAX_TOKENS）就够。
        # 先瘦身 → 后面的 check_overflow / compress 都基于"模型真正会收到的" token 量，
        # 与 compress 的 token 维度 keep_recent（resolve_keep_recent_by_tokens）一致。
        # 只动发给模型的副本，不碰盘上 transcript / 前端卡片 / 最近 protect_recent 条原文。
        slimmed_tail = slim_history_tool_results(history_tail)
        history = list(slimmed_tail)

        # 溢出检测（与 v1 相同逻辑，但现在基于已瘦身的 history）
        # checkpoint 虽然位于主 system 中，仍必须计入预算。
        budget_history = list(history)
        if active_checkpoint_summary:
            budget_history.insert(0, {
                "role": "system",
                "content": active_checkpoint_summary,
            })
        context_length = self.get_active_context_length()
        overflow = self.memory_manager.check_conversation_overflow(
            budget_history,
            context_length=context_length,
        )
        keep_recent = None
        if overflow["status"] == "overflow":
            keep_recent = CONVERSATION_KEEP_RECENT_OVERFLOW
        elif overflow["status"] == "warning":
            keep_recent = CONVERSATION_KEEP_RECENT_WARNING

        if keep_recent is not None:
            if compression_progress is not None:
                try:
                    await compression_progress({
                        "type": "system_notice",
                        "kind": "context_compaction_started",
                        "content": "正在把较早的对话整理为精简摘要…",
                        "payload": {
                            "status": overflow["status"],
                            "tokens_before": overflow.get("tokens"),
                            "threshold_warning": overflow.get("threshold_warning"),
                            "threshold_max": overflow.get("threshold_max"),
                        },
                    })
                except Exception:
                    logger.debug("context compaction start notification failed", exc_info=True)

            compression = await self.memory_manager.compress_conversation_history_detailed(
                slimmed_tail,
                self.llm_client,
                keep_recent=keep_recent,
                prior_summary=active_checkpoint_summary,
                original_non_system_count=sum(
                    1 for item in raw_history if item.get("role") != "system"
                ),
            )
            history = compression.history

            # 成功摘要进入主 system 的稳定 checkpoint 段；fallback 可能把旧检查点
            # 临时塞回 history。两种情况都从 history 中移除同一份摘要，避免重复发送。
            if compression.summary_note is not None:
                active_checkpoint_summary = str(
                    compression.summary_note.get("content") or ""
                ).strip() or active_checkpoint_summary
            if active_checkpoint_summary:
                for index, item in enumerate(history):
                    if (
                        item.get("role") == "system"
                        and str(item.get("content") or "").strip()
                        == active_checkpoint_summary
                    ):
                        history.pop(index)
                        break

            # 仅成功摘要才持久化；LLM 摘要失败时沿用本轮 trim 兜底，下一轮可重试，
            # 绝不把信息不完整的裁剪说明固化成长期检查点。
            if (
                checkpoint_manager is not None
                and session_id
                and compression.summary_note is not None
                and compression.covered_non_system_count > 0
            ):
                new_covered_count = _advance_history_index_by_non_system(
                    raw_history,
                    covered_message_count,
                    compression.covered_non_system_count,
                )
                old_generation = int((checkpoint or {}).get("generation") or 0)
                active_call = _active_call_config_var.get()
                summary_model = (
                    getattr(active_call, "model", None)
                    or getattr(self.llm_client, "model", None)
                )
                provisional_budget_history = list(history)
                if active_checkpoint_summary:
                    provisional_budget_history.insert(0, {
                        "role": "system",
                        "content": active_checkpoint_summary,
                    })
                provisional_after = self.memory_manager.check_conversation_overflow(
                    provisional_budget_history,
                    context_length=context_length,
                )
                candidate = make_context_checkpoint(
                    session_id=session_id,
                    history=raw_history,
                    covered_message_count=new_covered_count,
                    covered_non_system_count=sum(
                        1
                        for item in raw_history[:new_covered_count]
                        if item.get("role") != "system"
                    ),
                    summary=compression.summary_note["content"],
                    summary_data=compression.summary_data,
                    generation=old_generation + 1,
                    summary_model=summary_model,
                    tokens_before=overflow.get("tokens"),
                    tokens_after=provisional_after.get("tokens"),
                )
                try:
                    committed = await checkpoint_manager.commit_context_checkpoint(
                        session_id,
                        candidate,
                        raw_history,
                    )
                except Exception:
                    logger.warning(
                        "[CONTEXT_CHECKPOINT] commit failed session=%s",
                        session_id,
                        exc_info=True,
                    )
                    committed = None

                if committed is not None:
                    # 并发压缩时 commit 可能返回已存在的同源/更前进版本。
                    # 当前请求也改用已提交的字节，保证下一轮前缀完全一致。
                    committed_count = int(
                        committed.get("covered_message_count") or 0
                    )
                    active_checkpoint_summary = str(
                        committed.get("summary") or ""
                    ).strip() or active_checkpoint_summary
                    history = slim_history_tool_results(
                        raw_history[committed_count:]
                    )

            final_budget_history = list(history)
            if active_checkpoint_summary:
                final_budget_history.insert(0, {
                    "role": "system",
                    "content": active_checkpoint_summary,
                })
            after = self.memory_manager.check_conversation_overflow(
                final_budget_history,
                context_length=context_length,
            )
            if compression_progress is not None:
                try:
                    await compression_progress({
                        "type": "system_notice",
                        "kind": "context_compaction_completed",
                        "content": "较早的对话已整理，正在继续回答。",
                        "payload": {
                            "tokens_before": overflow.get("tokens"),
                            "tokens_after": after.get("tokens"),
                            "message_count_after": len(history),
                        },
                    })
                except Exception:
                    logger.debug("context compaction completion notification failed", exc_info=True)

        # 压缩可能在本轮生成或并发复用一个更新的检查点；正式调用模型前再次渲染
        # 主 system，确保本轮立即使用 static -> checkpoint -> dynamic 的稳定顺序。
        refreshed_base_system = _compose_system_prompt_with_checkpoint(
            prompt_data,
            active_checkpoint_summary,
        )
        refreshed_system_content = (
            f"{refreshed_base_system}\n\n{system_extra}"
            if system_extra
            else refreshed_base_system
        )
        if refreshed_system_content:
            if primary_system_message is None:
                primary_system_message = {
                    "role": "system",
                    "content": refreshed_system_content,
                }
                messages.insert(0, primary_system_message)
            else:
                primary_system_message["content"] = refreshed_system_content

        # 推断 provider_kind（未指定时自动推断）。
        # 优先级（2026-05-12 半截迁移补完）：
        #   1. ContextVar `_active_call_config_var`（chat handler `use_call_config` 注入）
        #      —— per-task 隔离，并发安全，跟 LLMClient._get_api_client / _get_effective_model
        #      读 cfg 的路径对齐。**这是新代码该走的路**。
        #   2. fallback：`self.llm_client` 单例字段（老路径；启动后没人 set_model 时
        #      就一直是 models.json 默认值，会跟当前 chat 真实路由对不上 ——
        #      事故 2026-05-12：用户切到 anthropic 反代但 LLMClient 单例字段还停在启动
        #      默认的 openai_chat，导致 build_messages 误判 provider_kind=openai_compat、
        #      image_ref 被退化为文字 placeholder、模型脑补幻觉描述图片。
        #      详见 _docs/handover_model_provider_pairing_full_audit_2026-05-12.md）。
        # 测试覆盖：tests_core/test_agent_build_messages.py::TestAutoProviderInferFromContextVar
        if provider_kind is None:
            cc = _active_call_config_var.get()
            if cc is not None:
                provider_kind = infer_provider_kind(
                    model=cc.model or "",
                    provider=cc.provider_id or "",
                    endpoint=cc.base_url or "",
                )
            else:
                llm = self.llm_client
                provider_kind = infer_provider_kind(
                    model=getattr(llm, "model", "") or "",
                    provider=getattr(llm, "provider", "") or "",
                    endpoint=getattr(llm, "base_url", "") or "",
                )

        # 历史消息归一化：history_entry_to_openai_std 处理：
        # 1. assistant content=list[ContentBlock] → OpenAI std (content + tool_calls)
        # 2. user content=list[tool_result blocks] → 拆成 N 条 role=tool
        # 3. 纯文本 user/assistant/system 直通
        # 一条 v3 entry 可能产出 1+N 条 OpenAI std 消息（assistant + 多个 tool）
        normalized_history: list[dict] = []
        for msg in history:
            normalized_history.extend(history_entry_to_openai_std(msg))

        # vision 跨轮持久化（Stage 1b + cline 流派 2）：transcript 里 image
        # base64 被换成轻量 image_ref（按 sha256 内容寻址），喂给 LLM 前在
        # 这里统一 reinflate 回真 image block。
        #
        # 流派 2 后 image_ref 主要出现在独立 user message.content 里
        # （cline 流派 2 新写盘格式：image 拆出来单独成一条 user message）；
        # 老 transcript 还可能在 tool_result.content 里 —— reinflate 一并处理。
        #
        # 协议适配：
        # - Anthropic 路径：reinflate 完直接喂给模型（image block 是原生格式）
        # - OpenAI 路径：reinflate 完后 to_openai_messages 在 user 分支把
        #   image block 转 image_url（OpenAI vision 协议）。模型本身不支持
        #   vision 时由模型 / 反代自行拒绝，agent 层不做能力探测。
        from .attachments import reinflate_image_refs_in_content
        for nm in normalized_history:
            content = nm.get("content")
            if isinstance(content, list):
                new_content, _missed = reinflate_image_refs_in_content(content, self.agent_id)
                nm["content"] = new_content

        # 按 provider 路由到对应 adapter
        if provider_kind == "anthropic":
            # to_anthropic_messages 返回 (system_str, anthropic_messages)
            # system_str 是从 normalized_history 中提取的 system 消息（通常为 None，
            # 因为 build_messages 自己已经处理 system，history 里不含 system）
            extra_system, anthropic_messages = to_anthropic_messages(normalized_history)
            if extra_system and messages and messages[0].get("role") == "system":
                # 把 adapter 提取的额外 system 合并进首个 system 消息
                messages[0]["content"] = (
                    messages[0]["content"] + "\n\n" + extra_system
                )
            messages.extend(anthropic_messages)
        else:
            # openai_compat / unknown → openai_compat adapter（返回纯 list）
            openai_messages = to_openai_messages(normalized_history)
            messages.extend(openai_messages)

        # 追加用户消息（已被 caller 提前持久化到 conversation_history 时跳过，
        # 避免 history 末尾 + 这里 append 造成的双 user 消息——主要用于 main.py /chat
        # inline 路径的 user 消息提前持久化方案，解决并发乱序 / 失忆 bug）
        if not user_message_already_in_history:
            messages.append({"role": "user", "content": user_message})

        prompt_cache_info = {
            "enable_caching": prompt_data["enable_caching"],
            "static": prompt_data["static"],
            "dynamic": prompt_data["dynamic"],
            "context_checkpoint": active_checkpoint_summary or "",
        }

        # 上下文构成估算：写进会话内存态，record_token_usage 发 SSE 时附带。
        # 放在 return 前——此时 messages / system / system_extra 都是本轮最终形态，
        # 估算结果即"本轮请求构成"。窗口大小复用现成的 get_active_context_length
        # （active call config 的 context_length / capabilities 能力表推断）。
        if session_id:
            try:
                from backend.runtime_model_config import _resolve_runtime_model_id
                # 模型名取本次请求实际路由的 model：优先 active call config
                # （chat handler 注入的 per-call 配置，含会话级 picker override），
                # 回落 _resolve_runtime_model_id（agent.active_model / global）。
                # 只用后者会在"会话切过模型"时显示角色默认模型而不是真用的那个。
                _cc = _active_call_config_var.get()
                _model_id = (
                    _cc.model if _cc is not None and getattr(_cc, "model", None) else None
                ) or _resolve_runtime_model_id(agent_id=self.agent_id)
                _breakdown = compute_context_breakdown(
                    base_system=refreshed_base_system or "",
                    system_extra=system_extra or "",
                    messages=messages,
                    agent_id=self.agent_id,
                    model_id=_model_id,
                    context_length=self.get_active_context_length(),
                )
                from backend.session import get_session_manager
                get_session_manager().set_latest_context_breakdown(session_id, _breakdown)
            except Exception:
                logger.debug("context breakdown computation failed", exc_info=True)

        return messages, prompt_cache_info

    async def chat(self, user_message: str, stream: bool = True,
                   session_id: str = None,
                   conversation_history: List[Dict[str, str]] = None,
                   system_extra: str = None,
                   recall_context: str = None,
                   defer_refine: bool = False,
                   user_message_already_in_history: bool = False) -> AsyncIterator[Dict]:
        """聊天（返回结构化事件流）

        v3 改造：
        - 不再将消息追加到 self.conversation_history
        - conversation_history 由调用方传入
        - 消息持久化由 main.py 通过 SessionManager 完成
        - Agent 只负责 LLM 调用和事件流返回

        v4 改造:
        - 支持 system_extra 参数，用于场景上下文注入
        """
        _agent_wd_token = None
        if session_id:
            try:
                from backend.tools.builtin.file_tools import _current_working_directory_var
                if _current_working_directory_var.get() is None:
                    from .session import get_session_manager
                    _sess_obj = await get_session_manager().get_session(session_id)
                    if _sess_obj:
                        _sess_wd = _sess_obj.get_pinned_workspace_directory()
                        if _sess_wd:
                            _agent_wd_token = _current_working_directory_var.set(_sess_wd)
            except Exception:
                pass

        try:
            if stream:
                # build_messages 里的 LLM 摘要发生在正式回答之前。把它放到独立 task，
                # 通过 queue 及时把“压缩中/已完成”事件送进现有聊天流；否则前端只能
                # 在整个摘要结束后才收到第一帧，看起来像无反馈卡顿。
                compression_events: asyncio.Queue = asyncio.Queue()

                async def _on_compression_progress(event: Dict[str, Any]) -> None:
                    await compression_events.put(event)

                build_task = asyncio.create_task(self.build_messages(
                    user_message,
                    session_id=session_id,
                    conversation_history=conversation_history,
                    system_extra=system_extra,
                    recall_context=recall_context,
                    user_message_already_in_history=user_message_already_in_history,
                    compression_progress=_on_compression_progress,
                ))
                try:
                    while not build_task.done():
                        progress_task = asyncio.create_task(compression_events.get())
                        done, _ = await asyncio.wait(
                            {build_task, progress_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if progress_task in done:
                            yield progress_task.result()
                        else:
                            progress_task.cancel()
                            await asyncio.gather(progress_task, return_exceptions=True)

                    messages, prompt_cache_info = await build_task
                    while not compression_events.empty():
                        yield compression_events.get_nowait()
                except BaseException:
                    if not build_task.done():
                        build_task.cancel()
                        await asyncio.gather(build_task, return_exceptions=True)
                    raise

                full_response = ""
                full_thinking = ""
                turn_events: List[Dict[str, Any]] = []
                import time as _time
                _turn_start_ts = _time.time()

                if self._should_use_v2_chain():
                    ctx = self._build_run_context(session_id=session_id)
                    event_iter = self._ensure_v2_lead().run_streaming(
                        messages, ctx,
                        session_id=session_id,
                        prompt_cache_info=prompt_cache_info,
                    )
                else:
                    event_iter = self.llm_client.chat(
                        messages, stream=True, use_tools=True,
                        agent_id=self.agent_id, session_id=session_id,
                        prompt_cache_info=prompt_cache_info,
                    )

                try:
                    async for event in event_iter:
                        etype = event.get("type", "content")
                        if etype == "thinking":
                            full_thinking += event.get("text", "")
                        elif etype == "content":
                            full_response += event.get("text", "")
                        if etype in ("tool_call", "tool_result", "system_notice"):
                            turn_events.append(dict(event))
                        yield event
                finally:
                    # 2026-05-15 修：close_stale_tasks 必须在 finally 里，否则用户按"停止"
                    # 触发 CancelledError 时会跳过它 → task 卡在 in_progress 永不收口，
                    # 前端 InlineTasksPanel 再进会话就一直转圈。
                    # 参考 worker.py:1886-1898 的兜底路径。
                    try:
                        from backend.tools.builtin.task_tools import get_task_manager
                        get_task_manager().close_stale_tasks(
                            session_id=session_id,
                            agent_id=self.agent_id,
                            cutoff_ts=_turn_start_ts,
                        )
                    except Exception as _exc:
                        logger.warning("[AGENT] close_stale_tasks failed: %s", _exc)

                # 记录到日记（Agent级别的长期记忆，不涉及对话历史）
                save_content = full_response
                if full_thinking:
                    save_content = f"## 思考过程\n\n{full_thinking}\n\n---\n\n{full_response}"
                if not defer_refine:
                    growth_result = self._log_conversation(
                        user_message,
                        save_content,
                        session_id=session_id,
                        turn_events=turn_events,
                        equipped_skills=self.skill_loader.get_equipped_skills(self.agent_id),
                        growth_response=full_response,
                    )
                    for notice in self._build_skill_growth_notices(growth_result):
                        yield notice
            else:
                collected = await self.run_collected(
                    user_message,
                    session_id=session_id,
                    conversation_history=conversation_history,
                    system_extra=system_extra,
                    recall_context=recall_context,
                    defer_refine=defer_refine,
                    collect_events=True,
                    user_message_already_in_history=user_message_already_in_history,
                )
                for event in collected.get("events", []):
                    payload = dict(event)
                    payload.pop("seq", None)
                    payload.pop("timestamp", None)
                    yield payload
        finally:
            if _agent_wd_token is not None:
                try:
                    from backend.tools.builtin.file_tools import _current_working_directory_var
                    _current_working_directory_var.reset(_agent_wd_token)
                except Exception:
                    pass

    async def run_collected(
        self,
        user_message: str,
        session_id: str = None,
        conversation_history: List[Dict[str, str]] = None,
        system_extra: str = None,
        recall_context: str = None,
        defer_refine: bool = False,
        collect_events: bool = False,
        use_tools: bool = True,
        user_message_already_in_history: bool = False,
    ) -> Dict[str, Any]:
        """Run one runtime turn and collect the final text plus optional event trace."""
        messages, prompt_cache_info = await self.build_messages(
            user_message,
            session_id=session_id,
            conversation_history=conversation_history,
            system_extra=system_extra,
            recall_context=recall_context,
            user_message_already_in_history=user_message_already_in_history,
        )
        if self._should_use_v2_chain():
            ctx = self._build_run_context(session_id=session_id)
            result = await self._ensure_v2_lead().run_collected(
                messages, ctx,
                collect_events=collect_events,
                use_tools=use_tools,
                session_id=session_id,
            )
        else:
            result = await self.run_messages_collected(
                messages,
                collect_events=collect_events,
                use_tools=use_tools,
                session_id=session_id,
            )
        response = result["text"]
        notice_events = list(result["notice_events"])
        events = list(result["events"])
        turn_events = list(result.get("trace_events") or [])

        if not defer_refine:
            growth_result = self._log_conversation(
                user_message,
                response,
                session_id=session_id,
                turn_events=turn_events,
                equipped_skills=self.skill_loader.get_equipped_skills(self.agent_id),
                growth_response=response,
            )
            growth_notices = self._build_skill_growth_notices(growth_result)
            notice_events.extend(growth_notices)
            if collect_events:
                next_seq = len(turn_events)
                for notice in growth_notices:
                    events.append({
                        "seq": next_seq,
                        "type": "system_notice",
                        "timestamp": datetime.now().isoformat(),
                        "kind": notice.get("kind"),
                        "content": notice.get("content", ""),
                    })
                    next_seq += 1

        return {
            "text": response,
            "notice_events": notice_events,
            "events": events,
        }

    async def run_messages_collected(
        self,
        messages: List[Dict[str, str]],
        *,
        collect_events: bool = False,
        use_tools: bool = True,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run explicit runtime messages through the shared LLM runtime without rebuilding prompts."""
        response = ""
        notice_events: List[Dict[str, Any]] = []
        events: List[Dict[str, Any]] = []
        trace_events: List[Dict[str, Any]] = []
        event_seq = 0

        def _record_event(event: Dict[str, Any]) -> None:
            nonlocal event_seq
            record = build_runtime_trace_event(event, seq=event_seq)
            trace_events.append(record)
            if collect_events:
                events.append(dict(record))
            event_seq += 1

        async for event in self.llm_client.chat(
            messages,
            stream=True,
            use_tools=use_tools,
            agent_id=self.agent_id,
            session_id=session_id,
        ):
            _record_event(event)
            etype = event.get("type", "content")
            if etype == "content":
                response += event.get("text", "")
            elif etype == "system_notice":
                notice_events.append(dict(event))

        return {
            "text": response,
            "notice_events": notice_events,
            "events": events,
            "trace_events": trace_events,
        }

    def _log_conversation(
        self,
        user_message: str,
        assistant_response: str,
        session_id: Optional[str] = None,
        message_span: Optional[List[int]] = None,
        turn_events: Optional[List[Dict[str, Any]]] = None,
        equipped_skills: Optional[List[str]] = None,
        growth_response: Optional[str] = None,
    ) -> Dict[str, Any]:
        """记录对话到记忆系统（v4 2026-05-22 起不再写 memory/{date}.md 日记）

        v4 改造：
        - **不再生成/写入 memory/{date}.md 逐字日记**。原因详见
          memory_manager.get_context_for_chat 的 docstring（按 agent+date 合并
          会跨 session 串号）。
        - 仍调 sync_turn 但不传 diary_entry，留 hook 给未来 provider 用
          （比如 cloud sync / 外部记忆库 metric 上报）。
        - 仍异步触发 MemoryRefinerV2 沉淀到 MEMORY.md（跨 session 长期记忆）。
        - 逐字层完全由 transcript.jsonl 承担（per-session 严格隔离）。
        """
        self.memory_manager.sync_turn(
            user_message=user_message,
            assistant_response=assistant_response,
            session_id=session_id,
            message_span=message_span,
            # diary_entry=None  ← v4 不再传，停止写入 memory/{date}.md
        )

        growth_result: Dict[str, Any] = {"updated_skills": [], "leveled_up": []}
        try:
            growth_result = get_skill_growth_manager(self.agent_id).record_turn(
                user_message=user_message,
                assistant_response=growth_response if growth_response is not None else assistant_response,
                turn_events=turn_events or [],
                equipped_skills=equipped_skills or self.skill_loader.get_equipped_skills(self.agent_id),
            )
        except Exception as e:
            logger.warning("[Agent:%s] skill growth update failed: %s", self.agent_id, e)

        # 异步触发记忆提炼（不阻塞聊天流程）
        self._trigger_refine(
            user_message,
            assistant_response,
            session_id=session_id,
            message_span=message_span,
        )
        return growth_result

    def _build_skill_growth_notices(self, growth_result: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        leveled_up = list((growth_result or {}).get("leveled_up") or [])
        if not leveled_up:
            return []

        notices: List[Dict[str, Any]] = []
        growth_manager = get_skill_growth_manager(self.agent_id)
        for skill_name in leveled_up:
            record = growth_manager.get_skill_record(skill_name)
            notices.append({
                "type": "system_notice",
                "kind": "skill_level_up",
                "content": f"技能成长：{skill_name} 已提升到 Lv{record['level']} {record['level_label']}。",
                "skill_name": skill_name,
                "level": record["level"],
                "level_label": record["level_label"],
                "xp": record["xp"],
            })
        return notices

    def _get_refiner(self) -> Optional[MemoryRefinerV2]:
        """延迟初始化记忆提炼器"""
        if self._refiner is None:
            try:
                self._refiner = MemoryRefinerV2(self.llm_client, self.memory_manager)
                logger.info("[Agent:%s] 记忆提炼器已初始化", self.agent_id)
            except Exception as e:
                logger.error("[Agent:%s] 提炼器初始化失败: %s", self.agent_id, e)
                return None
        return self._refiner

    def _trigger_refine(
        self,
        user_message: str,
        assistant_response: str,
        session_id: Optional[str] = None,
        message_span: Optional[List[int]] = None,
    ):
        """异步触发记忆提炼（带冷却控制，每5轮最多触发1次）"""
        # 冷却递减
        if self._refine_cooldown > 0:
            self._refine_cooldown -= 1
            return

        refiner = self._get_refiner()
        if not refiner:
            return

        try:
            import threading
            t = threading.Thread(
                target=self._run_refine,
                args=(refiner, user_message, assistant_response, session_id, message_span),
                daemon=True
            )
            t.start()
            # 设冷却：成功触发后5轮不再触发
            self._refine_cooldown = 4
        except Exception as e:
            logger.error("[Agent:%s] 提炼触发失败: %s", self.agent_id, e)

    def _run_refine(
        self,
        refiner: MemoryRefinerV2,
        user_msg: str,
        asst_msg: str,
        session_id: Optional[str] = None,
        message_span: Optional[List[int]] = None,
    ):
        """在线程中运行记忆提炼"""
        try:
            result = refiner.process_and_save(
                user_msg,
                asst_msg,
                session_id=session_id,
                message_span=message_span,
            )
            if result.get("memory_saved") or result.get("reflection_saved"):
                logger.info("[Agent:%s] ✅ 记忆提炼完成: memory=%s, reflection=%s", self.agent_id, result.get('memory_saved'), result.get('reflection_saved'))
            else:
                logger.info("[Agent:%s] ⏭️ 本次对话无需沉淀", self.agent_id)
        except Exception as e:
            logger.error("[Agent:%s] ⚠️ 提炼异常: %s", self.agent_id, e)

    def trigger_refine_for_turn(
        self,
        user_message: str,
        assistant_response: str,
        session_id: Optional[str] = None,
        message_span: Optional[List[int]] = None,
    ) -> None:
        self._trigger_refine(
            user_message,
            assistant_response,
            session_id=session_id,
            message_span=message_span,
        )

    def remember(self, content: str, memory_type: str = "diary"):
        """主动记忆"""
        if memory_type == "diary":
            self.memory_manager.append_to_diary(content)
        elif memory_type == "long_term":
            self.memory_manager.add_memory(content)
        elif memory_type == "event":
            self.memory_manager.add_event_log(content)

    def get_memory_report(self) -> Dict[str, Any]:
        """获取记忆系统诊断报告"""
        return self.memory_manager.get_memory_size_report()


# ===== 智能体实例缓存 =====
# Phase 2.2 改造：不再按 agent_id 缓存实例（会共享 conversation_history 导致串话）
# 现在按 agent_id 缓存但实例不持有 conversation_history，由调用方传入
_agent_instances: Dict[str, Agent] = {}


def get_agent(agent_id: str = None, config_dir: str = None) -> Agent:
    """获取智能体实例（按 agent_id 缓存）

    注意：Agent 实例不再持有 conversation_history，每次 chat 调用由外部传入。
    因此同一 agent_id 的实例可以安全地被多个 session 复用。
    
    Args:
        agent_id: 智能体 ID，默认 "default"
        config_dir: 全局配置目录（兼容旧版）
    """
    aid = agent_id or "default"
    if aid not in _agent_instances:
        _agent_instances[aid] = Agent(agent_id=aid, config_dir=config_dir)
    return _agent_instances[aid]


def clear_agent_cache(agent_id: str = None):
    """清除智能体缓存（配置变更后调用）"""
    if agent_id:
        _agent_instances.pop(agent_id, None)
    else:
        _agent_instances.clear()
