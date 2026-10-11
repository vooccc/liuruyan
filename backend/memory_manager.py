"""
记忆管理器 v3 — 支持多智能体隔离的记忆系统 + 记忆温度
每个智能体拥有独立的 MEMORY.md、日记目录和对话历史

v2: 基于 Claude Code 记忆系统源码分析改造
- 分层预算制：人格核心(必加载) + 记忆索引(截断加载) + 日记详情(按需选择)
- Token 估算：中英文混合计算
- MEMORY.md 硬限制：200行/25KB（来自 Claude Code 生产验证）
- 对话历史压缩：超阈值自动摘要

v3: 新增记忆温度系统
- 🔥燃烧: 反复想起的执念，始终加载
- 🌡️温热: 带情感的记忆，优先加载
- ❄️冷存: 事实记录，按需加载
- 温度自动衰减（燃→温→冷），重要标记可锁定
"""
import os
import json
import logging
import re
import time

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

from .memory_provider import BaseMemoryProvider, build_file_memory_provider

logger = logging.getLogger(__name__)


# ===== 从 Claude Code 移植的常量 =====
# 来源: memdir.ts 第35-38行
MAX_ENTRYPOINT_LINES = 200      # MEMORY.md 最大行数
MAX_ENTRYPOINT_BYTES = 25_000   # MEMORY.md 最大字节数

# 来源: autoCompact.ts 第62-65行（适配我们的上下文窗口）
CONVERSATION_WARNING_TOKENS = 30_000   # 对话历史超过此值发出警告
CONVERSATION_MAX_TOKENS = 50_000       # 对话历史硬上限，超过则触发压缩
CONVERSATION_KEEP_RECENT_WARNING = 40
CONVERSATION_KEEP_RECENT_OVERFLOW = 20
CONVERSATION_SUMMARY_SOURCE_TOKEN_LIMIT = 12_000
CONVERSATION_SUMMARY_MAX_LINES = 120


@dataclass(frozen=True)
class ConversationCompressionResult:
    """一次上下文压缩的结构化结果，供检查点持久化使用。"""

    history: List[Dict[str, Any]]
    summary_data: Optional[Dict[str, Any]] = None
    summary_note: Optional[Dict[str, str]] = None
    covered_non_system_count: int = 0
    recent_non_system_count: int = 0
    used_fallback: bool = False


# ── 安全上限封顶配置（对标生产最佳实践）─────────
# 防止 1M/2M 超长上下文无上限线性放大至 70~80 万 token 导致网关 TPM 熔断、
# 首字延迟暴增（TTFT > 30s）以及本地代理连接解密校验错误（如 bad record MAC）。
# - 小模型（32k/128k）：按 70% / 80% 动态收缩，严防爆窗；
# - 超大模型（200k/1M）：在安全上限（默认 70k / 85k）处被精准拦截触发结构化检查点。
DEFAULT_MAX_SAFE_COMPACT_WARNING = 70_000
DEFAULT_MAX_SAFE_COMPACT_OVERFLOW = 85_000


def resolve_conversation_thresholds(
    context_length: Optional[int] = None,
    max_ceiling: Optional[int] = None,
) -> Tuple[int, int]:
    """按当前模型窗口计算自动压缩阈值；兼顾小模型自适应与大模型物理/TPM安全封顶。"""
    if isinstance(context_length, int) and context_length > 0:
        ceiling = max_ceiling
        if ceiling is None:
            env_val = (os.getenv("LIURUYAN_MAX_COMPACT_THRESHOLD") or "").strip()
            ceiling = int(env_val) if env_val.isdigit() and int(env_val) > 0 else DEFAULT_MAX_SAFE_COMPACT_OVERFLOW

        warning_ratio = DEFAULT_MAX_SAFE_COMPACT_WARNING / DEFAULT_MAX_SAFE_COMPACT_OVERFLOW
        warning_ceiling = int(ceiling * warning_ratio)
        warning = min(int(context_length * 0.70), warning_ceiling)
        overflow = min(int(context_length * 0.80), ceiling)
        return warning, overflow
    return CONVERSATION_WARNING_TOKENS, CONVERSATION_MAX_TOKENS

# ── fix②（2026-06-09，压缩改 token 维度）─────────────────────────────
# 体检（_context_audit.py）坐实根因②：压缩"保留最近 N 条"是纯条数闸，若这
# N 条里混着几条 4~7 万 token 的 tool_result，压完仍 >> 窗口。这里给"最近
# 窗口"加 token 预算双闸：先按条数上限取，再按 token 预算回退条数，但不低于
# 地板，保证至少留住最新几轮上下文（也对齐 slim 的 protect_recent 语义）。
CONVERSATION_KEEP_RECENT_TOKEN_BUDGET = 25_000
CONVERSATION_KEEP_RECENT_MIN = 4

# ── 历史工具结果瘦身与微压缩（Microcompact）───────────────────────────
# 阶梯式微压缩（Microcompact）体系：
# - 阶梯 1（最近 protect_recent 条消息，默认 4 条）：完全保留原文，供模型当轮精准行动；
# - 阶梯 2（过渡期 intermediate_recent 条消息，默认 10 条）：单条截断至 1,200 token，采用首尾对称保留（头看命令，尾看堆栈）；
# - 阶梯 3（更久远历史，超过 10 条以前）：彻底清空长输出与脱水代码入参，释放 95% 以上的历史垃圾 token（单条 < 30 token）！
HISTORY_SLIM_PROTECT_RECENT = 4
HISTORY_SLIM_INTERMEDIATE_RECENT = 10
HISTORY_TOOL_RESULT_MAX_TOKENS = 1_200
HISTORY_MICROCOMPACT_MIN_SAVINGS = 100  # 本身小于 100 token 的微型输出不处理，避免越清空越长
HISTORY_TOOL_RESULT_CLEARED_MARKER = (
    "[历史工具输出已清空：调用已成功执行，详细结果已在本地 transcript 归档。]"
)

# 分层预算配置（单位: token）
MEMORY_BUDGET = {
    "identity_always": {
        # 三层人格架构：GENOME(0) → IDENTITY → SOUL(1) → PERSONALITY → REFLECTIONS(2) → USER
        "files": ["GENOME.md", "IDENTITY.md", "SOUL.md", "PERSONALITY.md", "REFLECTIONS.md", "USER.md"],
        "max_tokens": 4000,       # 人格核心总预算（增加了 GENOME + REFLECTIONS）
        "selectable": False,      # 不参与选择，永远加载
    },
    "memory_core": {
        "files": ["MEMORY.md"],
        "max_tokens": 2000,       # 记忆索引总预算（超了就截断）
        "selectable": False,      # 总是加载，但可能截断
    },
    "diary_today": {
        "max_tokens": 2000,       # 今日日记预算
        "max_lines": 100,         # 今日日记最大行数
        "selectable": False,      # 今日日记总是加载
    },
    "diary_recent": {
        "max_files": 3,           # 最近N天日记（不含今天）
        "max_tokens": 1500,       # 最近日记总预算
        "selectable": True,       # 按需选择
    },
    "special_files": {
        "files": ["60_seconds_life.md", "60_seconds_thoughts.md", "real_thinking_log.md"],
        "max_tokens": 1000,       # 特殊文件总预算
        "selectable": True,       # 按需选择
    },
}

# ===== 记忆温度系统 =====
# 温度级别定义（数值越大越"热"，加载优先级越高）
TEMP_BURNING = 3   # 🔥 燃烧 — 始终加载，不可衰减
TEMP_WARM = 2      # 🌡️ 温热 — 优先加载
TEMP_COLD = 1      # ❄️ 冷存 — 按需加载

# 温度标记正则：匹配行首或行尾的 <!-- temp:XX --> 标记
TEMP_TAG_PATTERN = re.compile(r'<!--\s*temp:\s*(burning|warm|cold|🔥|🌡️|❄️)\s*-->')

# 温度标记映射（支持 emoji 和文字两种写法）
TEMP_MAP = {
    "burning": TEMP_BURNING, "🔥": TEMP_BURNING,
    "warm": TEMP_WARM, "🌡️": TEMP_WARM,
    "cold": TEMP_COLD, "❄️": TEMP_COLD,
}

# 衰减周期：多少天后温度自动降一级
TEMP_DECAY_DAYS = 14

# 衰减扫描的最小间隔（秒）—— 防止频繁调用导致重复扫描
# 用独立的 _last_decay_ts 计时，不能用文件 mtime（save_to_memory 会频繁刷新 mtime 造成永久死锁）
TEMP_DECAY_MIN_INTERVAL_SEC = 3600

# 锁定标记：带此标记的记忆不参与衰减
TEMP_LOCKED = re.compile(r'<!--\s*locked\s*-->')

# 条目日期标题：### [YYYY-MM-DD] ... —— 用于温度衰减的"陈旧度"判定
ENTRY_DATE_PATTERN = re.compile(r'^###\s*\[(\d{4}-\d{2}-\d{2})\]')

# 在温度标签下方多少行内寻找日期标题（refiner 当前格式：标签紧跟空行 + ### [日期]）
TEMP_DATE_LOOKAHEAD = 5


def _find_entry_date(lines: List[str], start_idx: int) -> "datetime | None":
    """在 lines[start_idx+1 : start_idx+1+TEMP_DATE_LOOKAHEAD] 范围内寻找
    `### [YYYY-MM-DD]` 标题并解析出 datetime。

    找不到（或日期解析失败）→ 返回 None，调用方按"保守不衰减"处理。
    """
    end = min(len(lines), start_idx + 1 + TEMP_DATE_LOOKAHEAD)
    for j in range(start_idx + 1, end):
        m = ENTRY_DATE_PATTERN.match(lines[j])
        if not m:
            continue
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d")
        except ValueError:
            return None
    return None


def parse_line_temperature(line: str) -> int:
    """解析单行的记忆温度
    
    Returns:
        TEMP_BURNING / TEMP_WARM / TEMP_COLD (默认冷存)
    """
    match = TEMP_TAG_PATTERN.search(line)
    if match:
        temp_str = match.group(1).lower()
        return TEMP_MAP.get(temp_str, TEMP_COLD)
    return TEMP_COLD


def is_locked(line: str) -> bool:
    """检查该行是否被锁定（不受衰减影响）"""
    return bool(TEMP_LOCKED.search(line))


def classify_memory_lines(content: str) -> List[Tuple[int, str]]:
    """将 MEMORY.md 的每一行按温度分类
    
    Returns:
        [(temperature, line), ...] — 按 temperature 降序排列
    """
    if not content:
        return []
    
    lines = content.split("\n")
    classified = []
    
    for line in lines:
        temp = parse_line_temperature(line)
        classified.append((temp, line))
    
    # 按温度降序排列（燃烧在前，冷存在后）
    classified.sort(key=lambda x: x[0], reverse=True)
    return classified


def apply_temp_decay(content: str) -> Tuple[str, int]:
    """应用温度衰减逻辑
    
    规则：
    - 🔥燃烧 → 不衰减（除非手动降级）
    - 🌡️温热 → 超过 TEMP_DECAY_DAYS 天未更新且未锁定的 → 降到❄️
    - ❄️冷存 → 保持不变
    
    Returns:
        (衰减后的内容, 变更的行数)
    """
    if not content:
        return content, 0
    
    lines = content.split("\n")
    changed = 0
    now = datetime.now()
    cutoff = timedelta(days=TEMP_DECAY_DAYS)
    
    for i, line in enumerate(lines):
        temp = parse_line_temperature(line)
        
        if temp != TEMP_WARM:
            continue  # burning / cold 都不处理
        if is_locked(line):
            continue  # 锁定的不衰减
        
        match = TEMP_TAG_PATTERN.search(line)
        if not match:
            continue
        
        entry_date = _find_entry_date(lines, i)
        if entry_date is None:
            # 找不到日期：保守不动，避免把无日期块一刀切凉
            continue
        if (now - entry_date) < cutoff:
            continue  # 还没冷透
        
        old_tag = match.group(0)
        new_tag = "<!-- temp:cold -->"
        lines[i] = line.replace(old_tag, new_tag, 1)
        changed += 1
    
    return "\n".join(lines), changed


def estimate_tokens(text: str) -> int:
    """估算文本的 token 数（中英文混合）

    移植自 Claude Code microCompact.ts 的 roughTokenCountEstimation
    英文 ~4 chars/token, 中文 ~1.5-2 chars/token
    加 4/3 padding 做保守估计
    """
    if not text:
        return 0
    chinese_chars = len([c for c in text if '\u4e00' <= c <= '\u9fff'])
    other_chars = len(text) - chinese_chars
    raw = chinese_chars / 1.5 + other_chars / 4
    return int(raw * 4 / 3)


def truncate_entrypoint(content: str) -> Tuple[str, bool]:
    """截断 MEMORY.md 到安全范围

    移植自 Claude Code memdir.ts truncateEntrypointContent()
    先按行数截断，再按字节数截断

    Returns:
        (截断后内容, 是否被截断)
    """
    if not content:
        return content, False

    lines = content.strip().split('\n')
    was_line_truncated = len(lines) > MAX_ENTRYPOINT_LINES
    was_byte_truncated = len(content.encode('utf-8')) > MAX_ENTRYPOINT_BYTES

    if was_line_truncated:
        content = '\n'.join(lines[:MAX_ENTRYPOINT_LINES])

    # 按字节截断（在行边界处切割）
    content_bytes = content.encode('utf-8')
    if len(content_bytes) > MAX_ENTRYPOINT_BYTES:
        cut_at = content_bytes.rfind(b'\n', 0, MAX_ENTRYPOINT_BYTES)
        if cut_at > 0:
            content = content_bytes[:cut_at].decode('utf-8', errors='ignore')
        else:
            content = content_bytes[:MAX_ENTRYPOINT_BYTES].decode('utf-8', errors='ignore')

    if was_line_truncated or was_byte_truncated:
        content += "\n\n> ⚠️ MEMORY.md 已截断至安全范围，部分内容未加载。"

    return content, was_line_truncated or was_byte_truncated


def truncate_to_tokens(content: str, max_tokens: int) -> str:
    """将内容截断到指定 token 预算内

    按行切割，保留前面的内容（通常前面是索引和核心信息）
    """
    if not content:
        return content

    if estimate_tokens(content) <= max_tokens:
        return content

    lines = content.split('\n')
    result_lines = []
    current_tokens = 0

    for line in lines:
        line_tokens = estimate_tokens(line)
        if current_tokens + line_tokens > max_tokens:
            break
        result_lines.append(line)
        current_tokens += line_tokens

    result = '\n'.join(result_lines)
    if len(result_lines) < len(lines):
        result += f"\n\n> ⚠️ 内容已截断（预算 {max_tokens} tokens，实际约 {current_tokens} tokens）"

    return result


def _slim_tool_result_text(text: str, max_tokens: int) -> str:
    """把单条 tool_result 文本截到 token 上限，采用首尾对称保留（Head + Tail）算法。

    - 头部（约 25~30%）：保留启动命令、参数与环境初始信息；
    - 中间折叠：提示省略行数与原始 token 量，注明完整输出在 transcript，可用 session_search 召回；
    - 尾部（约 70~75%）：保留关键报错堆栈、异常类型、失败断言与退出状态码。
    """
    if not text:
        return text
    original_tokens = estimate_tokens(text)
    if original_tokens <= max_tokens:
        return text

    lines = text.split('\n')
    if len(lines) <= 2:
        # 单行或超短行大字符串兜底
        char_budget = max_tokens * 3
        head_chars = max(30, int(char_budget * 0.25))
        tail_chars = max(50, char_budget - head_chars)
        marker = (
            f"\n\n[历史工具结果已瘦身：原约 {original_tokens} tokens，"
            f"仅保留首尾 ~{max_tokens} tokens。完整内容在 transcript 里，"
            f"需要时用 session_search 召回。]\n\n"
        )
        if len(text) > head_chars + tail_chars + len(marker):
            return text[:head_chars] + marker + text[-tail_chars:]
        return text

    # 预留 marker 预算
    marker_estimate_tokens = 50
    body_budget = max(50, max_tokens - marker_estimate_tokens)
    head_budget = max(20, int(body_budget * 0.30))

    # 1. 抓取头部行
    head_lines: List[str] = []
    head_used = 0
    for line in lines:
        lt = estimate_tokens(line) + 1
        if head_used + lt > head_budget and head_lines:
            break
        head_lines.append(line)
        head_used += lt

    head_end_idx = len(head_lines)

    # 2. 抓取尾部行
    tail_budget = max(30, body_budget - head_used)
    tail_lines: List[str] = []
    tail_used = 0
    for line in reversed(lines[head_end_idx:]):
        lt = estimate_tokens(line) + 1
        if tail_used + lt > tail_budget and tail_lines:
            break
        tail_lines.append(line)
        tail_used += lt
    tail_lines.reverse()

    omitted_lines = max(0, len(lines) - len(head_lines) - len(tail_lines))
    marker = (
        f"\n\n[... 历史工具结果已瘦身：中间 {omitted_lines} 行已折叠（原约 {original_tokens} tokens，"
        f"保留首尾 ~{max_tokens} tokens）。完整内容在 transcript 里，"
        f"需要时用 session_search 召回。...]\n\n"
    )

    return '\n'.join(head_lines) + marker + '\n'.join(tail_lines)


def _microcompact_tool_result_text(
    text: str,
    tier: int,  # 1: protect, 2: intermediate slim, 3: microcompact clear
    max_tokens: int = HISTORY_TOOL_RESULT_MAX_TOKENS,
) -> str:
    """按生命周期阶梯对工具输出做分级压缩或清空。"""
    if not text:
        return text
    if tier == 1:
        return text

    tokens = estimate_tokens(text)
    if tier == 3:
        # 久远历史：若输出本身很短（如 "OK", "created", 状态码等），清空反而增加体积，保留原文
        if tokens <= HISTORY_MICROCOMPACT_MIN_SAVINGS:
            return text
        return HISTORY_TOOL_RESULT_CLEARED_MARKER

    # Tier 2: 过渡期按 token 截断
    if tokens <= max_tokens:
        return text
    return _slim_tool_result_text(text, max_tokens)


def _slim_tool_use_input(inp: Any) -> Any:
    """在 Tier 3（深层历史）中对 assistant 的 tool_use 参数做脱水。

    保留核心语义（如 path, file_path, command, query 等关键检索/定位标识），
    对 edit/write_to_file/patch 等工具携带的巨大代码正文（new_string/content 等）脱水，
    消除长会话中成千上万行陈旧代码参数对上下文的无效挤占。
    """
    if not isinstance(inp, dict):
        return inp

    code_payload_keys = {
        "ReplacementContent", "TargetContent", "CodeContent", "replacement",
        "old_string", "new_string", "content", "file_text", "patch", "diff",
        "replacement_chunks", "ReplacementChunks",
    }
    preserved_keys = {
        "path", "TargetFile", "file_path", "command", "CommandLine",
        "query", "Query", "SearchPath", "shell", "Action", "TaskId",
        "toolAction", "toolSummary", "StartLine", "EndLine", "offset", "limit",
    }

    modified = False
    new_inp = dict(inp)
    for k, v in inp.items():
        if k in preserved_keys:
            continue
        if k in code_payload_keys:
            if isinstance(v, str) and len(v) > 120:
                new_inp[k] = f"[code omitted: original {len(v)} chars]"
                modified = True
            elif isinstance(v, (list, dict)):
                new_inp[k] = "[chunk details omitted]"
                modified = True
        elif isinstance(v, str) and len(v) > 300:
            new_inp[k] = f"[omitted: original {len(v)} chars]"
            modified = True

    return new_inp if modified else inp


def slim_history_tool_results(
    history: List[Dict[str, Any]],
    max_result_tokens: int = HISTORY_TOOL_RESULT_MAX_TOKENS,
    protect_recent: int = HISTORY_SLIM_PROTECT_RECENT,
    intermediate_recent: int = HISTORY_SLIM_INTERMEDIATE_RECENT,
) -> List[Dict[str, Any]]:
    """对"发给模型的历史副本"里的旧 tool_result 及 tool_use 参数进行阶梯式瘦身与微压缩（Microcompact）。

    工业界上下文微压缩实践：
    - 最近 protect_recent 条消息：Tier 1 原汁原味完整保留（模型写代码需依赖最新上下文）；
    - 4 ~ intermediate_recent 条消息：Tier 2 截断至 max_result_tokens，保留前置关键信息；
    - 超过 intermediate_recent 条消息：Tier 3 彻底微压缩清空（Microcompact Cleared），消除长会话中
      累积的陈旧工具巨量占用；同时对 assistant 的 tool_use 参数做长代码脱水，解除老轮次对上下文的占用。

    保证：
    - 不修改盘上 transcript（只返回新 list，原 entry/block 浅拷贝后改）。
    - 不影响前端展示（前端走独立 SSE / 读盘路径）。
    - 最近 ``protect_recent`` 条不动 —— 模型完整看到最新工具输出能据此动手。
    - 图片等非 text block 原样保留。
    - 兼容 Anthropic content blocks 与 OpenAI role="tool" 消息格式。

    Returns:
        新的 history list（仅被瘦身的 entry/block 是新对象，其余原样引用）。
    """
    if not history or max_result_tokens <= 0:
        return history

    n = len(history)
    out: List[Dict[str, Any]] = []
    for idx, entry in enumerate(history):
        dist = n - 1 - idx
        if dist < protect_recent:
            tier = 1
        elif dist < intermediate_recent:
            tier = 2
        else:
            tier = 3

        if tier == 1:
            out.append(entry)
            continue

        # 0. 兼容 assistant 角色：Tier 3 下对陈旧 tool_use 参数进行长代码/巨载荷脱水
        if entry.get("role") == "assistant":
            if tier != 3:
                out.append(entry)
                continue

            # a. Anthropic content blocks: [{"type": "tool_use", "id": "...", "input": {...}}]
            content = entry.get("content")
            if isinstance(content, list):
                new_blocks = None
                for b_idx, block in enumerate(content):
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        inp = block.get("input")
                        slimmed_inp = _slim_tool_use_input(inp)
                        if slimmed_inp is not inp and slimmed_inp != inp:
                            if new_blocks is None:
                                new_blocks = list(content)
                            new_blocks[b_idx] = {**block, "input": slimmed_inp}
                if new_blocks is not None:
                    out.append({**entry, "content": new_blocks})
                    continue

            # b. OpenAI tool_calls: [{"id": "...", "function": {"name": "...", "arguments": "{...}"}}]
            tool_calls = entry.get("tool_calls")
            if isinstance(tool_calls, list):
                new_calls = None
                for c_idx, call in enumerate(tool_calls):
                    if isinstance(call, dict) and isinstance(call.get("function"), dict):
                        fn = call["function"]
                        raw_args = fn.get("arguments")
                        if isinstance(raw_args, str) and len(raw_args) > 120:
                            try:
                                parsed = json.loads(raw_args)
                                if isinstance(parsed, dict):
                                    slimmed = _slim_tool_use_input(parsed)
                                    if slimmed != parsed:
                                        if new_calls is None:
                                            new_calls = list(tool_calls)
                                        new_fn = {**fn, "arguments": json.dumps(slimmed, ensure_ascii=False)}
                                        new_calls[c_idx] = {**call, "function": new_fn}
                            except Exception:
                                pass
                if new_calls is not None:
                    out.append({**entry, "tool_calls": new_calls})
                    continue

            out.append(entry)
            continue

        # 1. 兼容 OpenAI 格式：{"role": "tool", "content": "..."}
        if entry.get("role") == "tool":
            tr = entry.get("content", "")
            if isinstance(tr, str):
                slimmed = _microcompact_tool_result_text(tr, tier, max_result_tokens)
                if slimmed != tr:
                    out.append({**entry, "content": slimmed})
                    continue
            out.append(entry)
            continue

        # 2. 兼容 Anthropic v3 格式：{"role": "user", "content": [{"type": "tool_result", ...}]}
        content = entry.get("content")
        if not isinstance(content, list):
            out.append(entry)
            continue

        new_blocks = None  # 惰性拷贝：没改动就不建新对象
        for b_idx, block in enumerate(content):
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tr = block.get("content", "")
            if isinstance(tr, str):
                slimmed = _microcompact_tool_result_text(tr, tier, max_result_tokens)
                if slimmed is not tr and slimmed != tr:
                    if new_blocks is None:
                        new_blocks = list(content)
                    new_blocks[b_idx] = {**block, "content": slimmed}
            elif isinstance(tr, list):
                # list 形态：只瘦其中的 text block，图片等原样
                inner_new = None
                for i_idx, inner in enumerate(tr):
                    if (
                        isinstance(inner, dict)
                        and inner.get("type") == "text"
                        and isinstance(inner.get("text"), str)
                    ):
                        slimmed = _microcompact_tool_result_text(inner["text"], tier, max_result_tokens)
                        if slimmed != inner["text"]:
                            if inner_new is None:
                                inner_new = list(tr)
                            inner_new[i_idx] = {**inner, "text": slimmed}
                if inner_new is not None:
                    if new_blocks is None:
                        new_blocks = list(content)
                    new_blocks[b_idx] = {**block, "content": inner_new}

        if new_blocks is not None:
            out.append({**entry, "content": new_blocks})
        else:
            out.append(entry)
    return out


def resolve_keep_recent_by_tokens(
    messages: List[Dict[str, Any]],
    max_count: int,
    token_budget: int = CONVERSATION_KEEP_RECENT_TOKEN_BUDGET,
    floor: int = CONVERSATION_KEEP_RECENT_MIN,
) -> int:
    """决定"保留最近多少条"——条数上限与 token 预算双闸，取更紧者，但不低于 floor。

    动机（fix②，handover 根因②）：纯按条数保留会被"单条巨大 tool_result"击穿
    （最近 20 条里几条 4~7 万 token，压完仍爆窗）。这里从最新一条往回累加 token，
    超预算就停；但至少保留 ``floor`` 条，避免把最新上下文（含活跃任务/工具结果）
    也丢光。``max_count`` 是硬上限（对齐 warning/overflow 的既定条数）。

    Args:
        messages: 非 system 消息列表（时间正序，末尾最新）。
        max_count: 条数硬上限（如 CONVERSATION_KEEP_RECENT_OVERFLOW）。
        token_budget: 最近窗口的 token 预算。
        floor: 至少保留的条数地板。

    Returns:
        建议保留的"最近 N 条"数量（0 <= N <= min(max_count, len)）。
    """
    n = len(messages)
    if n == 0:
        return 0
    cap = min(max_count, n)
    eff_floor = min(max(0, floor), cap)
    used = 0
    kept = 0
    for msg in reversed(messages):
        if kept >= cap:
            break
        used += estimate_tokens(json.dumps(msg, ensure_ascii=False))
        # 已超预算且已满足地板 → 停在这里（不含当前这条）。
        if used > token_budget and kept >= eff_floor:
            break
        kept += 1
    return max(kept, eff_floor)


def truncate_to_lines(content: str, max_lines: int) -> str:
    """将内容截断到指定行数内"""
    if not content:
        return content

    lines = content.split('\n')
    if len(lines) <= max_lines:
        return content

    result = '\n'.join(lines[:max_lines])
    result += f"\n\n> ⚠️ 内容已截断至 {max_lines} 行"

    return result


class MemoryManager:
    """管理智能体的记忆系统（支持多智能体隔离）

    v2 改造要点:
    1. get_context_for_chat() 增加预算控制，不再无限制加载
    2. MEMORY.md 使用 truncate_entrypoint() 硬限制
    3. 日记文件按预算加载，超限截断
    4. 对话历史增加 token 检查和压缩触发
    """

    def __init__(
        self,
        agent_id: str = None,
        base_dir: str = None,
        provider: Optional[BaseMemoryProvider] = None,
    ):
        """初始化记忆管理器

        Args:
            agent_id: 智能体 ID，为 None 时使用项目根目录（兼容旧版）
            base_dir: 基础目录（仅在 agent_id 为 None 时使用）
        """
        self.agent_id = agent_id
        self.provider = provider or build_file_memory_provider(
            agent_id=agent_id,
            base_dir=base_dir,
        )
        self.base_dir = self.provider.base_dir
        self.memory_dir = self.provider.memory_dir
        self.long_term_memory_file = self.provider.long_term_memory_file
        self.conversation_history_file = self.provider.conversation_history_file
        self.reflections_file = self.provider.reflections_file
        # 衰减扫描节流：进程内累计的上次扫描完成时间戳
        # 进程重启会丢失，但启动钩子会立刻补一次扫描，闭环不出现盲区
        self._last_decay_ts: float = 0.0

    # ===== 基础读写方法（保持不变） =====

    def get_today_diary_path(self) -> Path:
        """获取今天的日记文件路径"""
        return self.provider.get_today_diary_path()

    def read_diary(self, date: str = None) -> str:
        """读取指定日期的日记，默认今天"""
        return self.provider.read_diary(date)

    def write_diary(self, content: str, date: str = None, append: bool = True):
        """写入日记"""
        self.provider.write_diary(content, date=date, append=append)

    def append_to_diary(self, entry: str):
        """追加一条日记记录"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        formatted_entry = f"\n### [{timestamp}]\n{entry}\n"
        self.provider.append_to_diary_entry(formatted_entry)

    def read_long_term_memory(self) -> str:
        """读取长期记忆"""
        return self.provider.read_long_term_memory()

    def write_long_term_memory(self, content: str) -> None:
        """覆盖写入长期记忆。"""
        self.provider.write_long_term_memory(content)

    def update_long_term_memory(self, content: str, section: str = None):
        """更新长期记忆"""
        existing = self.read_long_term_memory()

        if section:
            new_content = f"\n\n{content}"
            self.write_long_term_memory(existing + new_content)
        else:
            self.write_long_term_memory(content)

    def read_reflections(self) -> str:
        """读取行为洞察。"""
        return self.provider.read_reflections()

    def write_reflections(self, content: str) -> None:
        """写入行为洞察。"""
        self.provider.write_reflections(content)

    def list_diary_files(self) -> List[Path]:
        """列出全部日记文件。"""
        return self.provider.list_diary_files()

    def prefetch(self, **kwargs) -> Dict[str, Any]:
        """预取后端可能需要的记忆数据。"""
        return self.provider.prefetch(**kwargs)

    def sync_turn(
        self,
        *,
        user_message: str,
        assistant_response: str,
        session_id: Optional[str] = None,
        message_span: Optional[List[int]] = None,
        diary_entry: Optional[str] = None,
    ) -> None:
        """同步本轮对话到后端。"""
        self.provider.sync_turn(
            user_message=user_message,
            assistant_response=assistant_response,
            session_id=session_id,
            message_span=message_span,
            diary_entry=diary_entry,
        )

    def on_session_end(
        self,
        *,
        session_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """会话收尾钩子。"""
        self.provider.on_session_end(session_id=session_id, metadata=metadata)

    def add_event_log(self, event: str):
        """添加事件日志到 MEMORY.md"""
        today = datetime.now().strftime("%Y-%m-%d")
        log_entry = f"- **{today}**：{event}"

        existing = self.read_long_term_memory()

        # 查找事件日志部分并插入
        if "## 事件日志" in existing:
            lines = existing.split("\n")
            new_lines = []
            inserted = False

            for i, line in enumerate(lines):
                new_lines.append(line)
                if line.startswith("## 事件日志") and not inserted:
                    if i + 1 < len(lines) and lines[i + 1].startswith("-"):
                        new_lines.append(log_entry)
                        inserted = True

            if not inserted:
                new_lines.append(log_entry)

            self.write_long_term_memory("\n".join(new_lines))

    def add_memory(self, memory: str):
        """添加沉淀记忆到 MEMORY.md"""
        existing = self.read_long_term_memory()

        if "## 记忆" in existing:
            lines = existing.split("\n")
            new_lines = []
            inserted = False

            for i, line in enumerate(lines):
                new_lines.append(line)
                if line.startswith("## 记忆") and not inserted:
                    new_lines.append(f"- **{memory}**")
                    inserted = True

            if not inserted:
                new_lines.append(f"- **{memory}**")

            self.write_long_term_memory("\n".join(new_lines))

    # ===== 核心改造：带预算的上下文构建 =====

    def get_context_for_chat(self, budget_tokens: int = 8000) -> str:
        """获取用于聊天的记忆上下文（带预算控制 + 温度排序）

        v3 改造:
        - v2: 分层预算制、MEMORY.md 截断、日记按需加载
        - v3: MEMORY.md 按记忆温度排序后截断（🔥燃烧优先，❄️冷存最后）
        - v4 (2026-05-22): **废除 diary 注入**。memory/{date}.md 是逐字日记，
          按 agent+date 合并、无 session 隔离，被注入会导致跨会话串号
          （在群聊 B 里能复述群聊 A 的原话）。逐字层的职责完全交给
          ``transcript.jsonl``（按 session 严格隔离）；跨会话延续靠
          MEMORY.md 提炼（MemoryRefinerV2 异步沉淀）+ recall_memory /
          session_search 工具按需召回。旧的 memory/{date}.md 文件保留在
          盘上做归档但不再读取/写入。详见
          docs/architecture_spec.md（待补）。

        Args:
            budget_tokens: 记忆上下文总 token 预算，默认 8000

        Note:
            人格核心文件(IDENTITY/SOUL/PERSONALITY/USER)由 config.py 的
            build_system_prompt() 负责加载，此处不再重复加载，避免双重注入。
        """
        context_parts = []
        remaining_budget = budget_tokens

        # 1. 长期记忆（MEMORY.md）— 温度排序后截断
        memory_budget = MEMORY_BUDGET["memory_core"]["max_tokens"]
        long_term = self.read_long_term_memory()
        if long_term:
            # v3: 按温度排序（燃烧→温热→冷存）
            classified = classify_memory_lines(long_term)

            # 分离标题行和内容行（标题行保持在前）
            header_lines = []
            content_lines = []
            for temp, line in classified:
                stripped = line.strip()
                if stripped.startswith("#") and not stripped.startswith("<!--"):
                    header_lines.append((temp, line))
                else:
                    content_lines.append((temp, line))

            # 重构：标题 + 已按温排序的内容
            reordered = header_lines + content_lines
            sorted_content = "\n".join(line for _, line in reordered)

            truncated, was_truncated = truncate_entrypoint(sorted_content)
            truncated = truncate_to_tokens(truncated, min(memory_budget, remaining_budget))
            context_parts.append(f"## 长期记忆\n{truncated}")
            remaining_budget -= estimate_tokens(truncated)

        # v4 (2026-05-22): 已废除"今日日记"和"近期日记"注入段。
        # 原因：memory/{date}.md 按 agent+date 合并所有 session 的逐字摘录，
        # 注入到 system prompt 会让模型在新会话里看到旧会话的原话，造成串号。
        # 替代方案：transcript.jsonl（per-session 严格隔离） + MEMORY.md 提炼
        # （跨 session 长期记忆） + recall_memory/session_search 工具（按需召回）。

        result = "\n\n---\n\n".join(context_parts)

        # 最终安全检查：总 token 不超预算
        if estimate_tokens(result) > budget_tokens:
            result = truncate_to_tokens(result, budget_tokens)

        return result

    def get_memory_size_report(self) -> Dict[str, Any]:
        """获取记忆系统各文件的大小报告

        用于诊断和监控，不注入到 prompt 中
        """
        report = {
            "agent_id": self.agent_id,
            "files": {},
            "conversation_history_tokens": 0,
            "total_tokens_estimate": 0,
        }

        # 检查 MEMORY.md
        memory_content = self.read_long_term_memory()
        if memory_content:
            report["files"]["MEMORY.md"] = {
                "bytes": len(memory_content.encode('utf-8')),
                "lines": len(memory_content.split('\n')),
                "tokens": estimate_tokens(memory_content),
                "exceeds_limit": len(memory_content.split('\n')) > MAX_ENTRYPOINT_LINES
                                 or len(memory_content.encode('utf-8')) > MAX_ENTRYPOINT_BYTES,
            }

        # 检查日记文件
        for f in self.list_diary_files():
            content = f.read_text(encoding="utf-8")
            report["files"][f"memory/{f.name}"] = {
                "bytes": len(content.encode('utf-8')),
                "lines": len(content.split('\n')),
                "tokens": estimate_tokens(content),
            }

        # 检查对话历史
        history = self.load_conversation_history()
        if history:
            history_text = json.dumps(history, ensure_ascii=False)
            report["conversation_history_tokens"] = estimate_tokens(history_text)
            report["conversation_history_messages"] = len(history)

        # 总估算
        total = sum(f["tokens"] for f in report["files"].values())
        total += report["conversation_history_tokens"]
        report["total_tokens_estimate"] = total

        return report

    # ===== 记忆温度操作 =====

    def tag_memory_entry(self, entry_pattern: str, temperature: str = "warm") -> bool:
        """给匹配的 MEMORY.md 条目添加温度标记
        
        Args:
            entry_pattern: 匹配条目的关键字（模糊匹配，包含该字串的行会被标记）
            temperature: "burning"/"warm"/"cold"
        
        Returns:
            是否成功修改
        """
        content = self.read_long_term_memory()
        if not content:
            return False
        
        lines = content.split("\n")
        modified = False
        
        for i, line in enumerate(lines):
            if not line.strip() or line.strip().startswith("#"):
                continue
            if entry_pattern in line:
                # 如果已有温度标记则替换
                if TEMP_TAG_PATTERN.search(line):
                    temp_emoji = {"burning": "🔥", "warm": "🌡️", "cold": "❄️"}
                    new_tag = f"<!-- temp:{temp_emoji.get(temperature, '🌡️')} -->"
                    lines[i] = TEMP_TAG_PATTERN.sub(new_tag, line)
                else:
                    lines[i] = f"<!-- temp:{temperature} -->{line}"
                modified = True
        
        if modified:
            self.write_long_term_memory("\n".join(lines))
        
        return modified

    def apply_temp_decay(self, force: bool = False) -> int:
        """对当前智能体的 MEMORY.md 执行温度衰减

        Args:
            force: 强制扫描，绕过 TEMP_DECAY_MIN_INTERVAL_SEC 节流闸
                   （A 启动钩子可传 True 确保启动必跑；C refiner 钩子保持默认 False）

        Returns:
            变更的行数（被节流早退时返回 0）
        """
        # 节流闸：距上次扫描不到 TEMP_DECAY_MIN_INTERVAL_SEC 秒就直接早退
        # 早退分支无 IO 无计算，多余调用零成本——所以上层可以无脑调用
        now = time.time()
        if not force and (now - self._last_decay_ts) < TEMP_DECAY_MIN_INTERVAL_SEC:
            return 0

        content = self.read_long_term_memory()
        if not content:
            self._last_decay_ts = now
            return 0

        decayed, changed = apply_temp_decay(content)
        if changed > 0:
            self.write_long_term_memory(decayed)
            logger.info("[MemoryManager] 温度衰减完成: 变更 %s 行", changed)

        # 无论是否变更都更新时间戳——"我扫过了"是事实，节流的语义是"避免重复扫"
        self._last_decay_ts = now
        return changed

    def get_temp_stats(self) -> Dict[str, Any]:
        """获取记忆温度统计"""
        content = self.read_long_term_memory()
        if not content:
            return {"total": 0, "burning": 0, "warm": 0, "cold": 0, "unlabeled": 0}
        
        classified = classify_memory_lines(content)
        stats = {"total": len(classified), "burning": 0, "warm": 0, "cold": 0, "unlabeled": 0}
        
        for temp, line in classified:
            if temp == TEMP_BURNING:
                stats["burning"] += 1
            elif temp == TEMP_WARM:
                stats["warm"] += 1
            else:
                if TEMP_TAG_PATTERN.search(line):
                    stats["cold"] += 1
                else:
                    stats["unlabeled"] += 1
        
        return stats

    # ===== 对话历史持久化 + 压缩触发 =====

    def save_conversation_history(self, history: List[Dict[str, str]]) -> None:
        """保存对话历史到文件"""
        try:
            self.provider.save_conversation_history(history)
        except Exception as e:
            logger.error("[MemoryManager] 保存对话历史失败: %s", e)

    def load_conversation_history(self) -> List[Dict[str, str]]:
        """从文件加载对话历史"""
        try:
            return self.provider.load_conversation_history()
        except Exception as e:
            logger.error("[MemoryManager] 加载对话历史失败: %s", e)
            return []

    def check_conversation_overflow(
        self,
        history: List[Dict[str, str]],
        context_length: Optional[int] = None,
    ) -> Dict[str, Any]:
        """检查对话历史是否溢出，返回压缩建议

        移植自 Claude Code autoCompact.ts 的阈值计算逻辑

        Returns:
            {
                "status": "ok" | "warning" | "overflow",
                "tokens": int,
                "threshold_warning": int,
                "threshold_max": int,
                "suggested_action": str  # "none" | "trim_old" | "summarize"
            }
        """
        history_text = json.dumps(history, ensure_ascii=False)
        tokens = estimate_tokens(history_text)
        warning_tokens, max_tokens = resolve_conversation_thresholds(context_length)

        if tokens > max_tokens:
            return {
                "status": "overflow",
                "tokens": tokens,
                "threshold_warning": warning_tokens,
                "threshold_max": max_tokens,
                "suggested_action": "summarize",
            }
        elif tokens > warning_tokens:
            return {
                "status": "warning",
                "tokens": tokens,
                "threshold_warning": warning_tokens,
                "threshold_max": max_tokens,
                "suggested_action": "trim_old",
            }
        else:
            return {
                "status": "ok",
                "tokens": tokens,
                "threshold_warning": warning_tokens,
                "threshold_max": max_tokens,
                "suggested_action": "none",
            }

    def _format_history_for_summary(self, messages: List[Dict[str, str]]) -> str:
        """把待压缩的历史消息整理成适合摘要的纯文本。"""
        lines = []
        for idx, msg in enumerate(messages, 1):
            role = msg.get("role", "user")
            role_label = {
                "user": "用户",
                "assistant": "助手",
                "system": "系统",
            }.get(role, role)
            content = msg.get("content", "")
            if not isinstance(content, str):
                content = str(content)
            content = content.strip()
            if not content:
                continue
            lines.append(f"[{idx}] {role_label}: {content}")

        if not lines:
            return ""

        joined = "\n".join(lines)
        joined = truncate_to_lines(joined, CONVERSATION_SUMMARY_MAX_LINES)
        joined = truncate_to_tokens(joined, CONVERSATION_SUMMARY_SOURCE_TOKEN_LIMIT)
        return joined

    def _extract_summary_json(self, text: str) -> Dict[str, Any]:
        """从模型输出中解析结构化摘要 JSON。"""
        if not text:
            return {}

        text = text.strip()
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return {}

        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _render_summary_note(self, summary: Dict[str, Any], original_count: int, keep_recent: int) -> str:
        """把结构化摘要渲染成注入历史用的 system note。"""
        active_task = str(summary.get("active_task", "")).strip() or "未明确"
        emotional_state = str(summary.get("emotional_state", "")).strip() or "未突出"

        def _normalize_items(items: Any) -> List[str]:
            if isinstance(items, list):
                return [str(item).strip() for item in items if str(item).strip()]
            if isinstance(items, str) and items.strip():
                return [items.strip()]
            return []

        key_facts = _normalize_items(summary.get("key_facts"))
        decisions = _normalize_items(summary.get("decisions"))
        open_threads = _normalize_items(summary.get("open_threads"))

        parts = [
            f"[系统] 早期对话已压缩为摘要（原共 {original_count} 条非系统消息，保留最近 {keep_recent} 条原文）。",
            "",
            "## 压缩摘要",
            f"- 当前任务：{active_task}",
            f"- 情绪/关系状态：{emotional_state}",
            "- 关键事实：",
        ]
        parts.extend([f"  - {item}" for item in key_facts[:5]] or ["  - 无"])
        parts.append("- 重要决定：")
        parts.extend([f"  - {item}" for item in decisions[:4]] or ["  - 无"])
        parts.append("- 未完成事项：")
        parts.extend([f"  - {item}" for item in open_threads[:4]] or ["  - 无"])
        parts.append("")
        parts.append("以上为早期上下文摘要，仅供延续任务和关系语义，不代表新的用户输入。")
        return "\n".join(parts)

    async def compress_conversation_history(
        self,
        history: List[Dict[str, str]],
        llm_client,
        keep_recent: int = CONVERSATION_KEEP_RECENT_OVERFLOW,
    ) -> List[Dict[str, str]]:
        """兼容旧调用方：返回压缩后的 history 列表。"""
        result = await self.compress_conversation_history_detailed(
            history,
            llm_client,
            keep_recent=keep_recent,
        )
        return result.history

    async def compress_conversation_history_detailed(
        self,
        history: List[Dict[str, Any]],
        llm_client,
        keep_recent: int = CONVERSATION_KEEP_RECENT_OVERFLOW,
        *,
        prior_summary: Optional[str] = None,
        original_non_system_count: Optional[int] = None,
    ) -> ConversationCompressionResult:
        """压缩旧消息并返回可持久化的摘要边界元数据。

        fix②（2026-06-09）：``keep_recent`` 不再是纯条数。先用
        ``resolve_keep_recent_by_tokens`` 按 token 预算把"最近 N 条"收紧到不爆窗
        （条数上限仍是传入的 ``keep_recent``，地板 CONVERSATION_KEEP_RECENT_MIN），
        再据此切 older/recent，避免"20 条里几条巨型 tool_result"压完仍 >> 窗口。

        ``prior_summary`` 是已经持久化的旧检查点。它只作为摘要输入，不与新摘要
        同时留在模型上下文里，避免每次压缩多叠一层 system message。
        """
        history = self.provider.on_pre_compress(history) or history

        system_msgs = [m for m in history if m.get("role") == "system"]
        non_system = [m for m in history if m.get("role") != "system"]
        prior_message = (
            {"role": "system", "content": prior_summary.strip()}
            if isinstance(prior_summary, str) and prior_summary.strip()
            else None
        )

        # token 维度收紧"最近窗口"——条数上限 keep_recent，但 token 超预算就少留几条。
        effective_keep = resolve_keep_recent_by_tokens(non_system, keep_recent)
        if len(non_system) <= effective_keep:
            preserved = list(system_msgs)
            if prior_message is not None:
                preserved.append(prior_message)
            preserved.extend(non_system)
            return ConversationCompressionResult(
                history=preserved,
                recent_non_system_count=len(non_system),
            )

        older_messages = non_system[:-effective_keep]
        recent_messages = non_system[-effective_keep:]
        summary_source = list(older_messages)
        if prior_message is not None:
            summary_source.insert(0, {
                "role": "system",
                "content": "以下是更早历史的已验证检查点，请与新增旧消息合并：\n\n"
                + prior_message["content"],
            })
        older_text = self._format_history_for_summary(summary_source)

        def fallback_result() -> ConversationCompressionResult:
            trimmed = self.trim_conversation_history(
                history,
                keep_recent=effective_keep,
            )
            if prior_message is not None:
                trimmed = (
                    trimmed[:len(system_msgs)]
                    + [prior_message]
                    + trimmed[len(system_msgs):]
                )
            return ConversationCompressionResult(
                history=trimmed,
                recent_non_system_count=len(recent_messages),
                used_fallback=True,
            )

        if not older_text:
            return fallback_result()

        response = await llm_client.chat_completion_async([
            {
                "role": "system",
                "content": (
                    "你是一个对话上下文压缩器。你的任务不是继续聊天，而是把较早对话压成结构化摘要，"
                    "供同一个角色稍后继续回答用户。请严格输出 JSON，不要输出额外解释。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "请压缩下面的较早对话，生成一个结构化摘要。\n\n"
                    "要求：\n"
                    "1. 只总结已经发生过的内容，不新增推测。\n"
                    "2. 优先保留：当前任务、用户明确提出的事实、重要决定、情绪/关系变化、未完成事项。\n"
                    "3. 不要复述无关闲聊，不要逐句重写。\n"
                    "4. 输出必须是 JSON，字段如下：\n"
                    "{\n"
                    '  "active_task": "一句话说明当前仍在推进的任务或主题，没有则写未明确",\n'
                    '  "key_facts": ["最多5条关键事实"],\n'
                    '  "decisions": ["最多4条已经做出的决定或结论"],\n'
                    '  "emotional_state": "一句话概括情绪、关系或互动状态",\n'
                    '  "open_threads": ["最多4条仍未完成或后续可能继续的话题"]\n'
                    "}\n\n"
                    f"待压缩对话：\n{older_text}\n"
                ),
            },
        ])
        summary = self._extract_summary_json(response.get("content", ""))
        if not summary:
            return fallback_result()

        summary_note = {
            "role": "system",
            "content": self._render_summary_note(
                summary,
                original_non_system_count or len(non_system),
                effective_keep,
            ),
        }
        return ConversationCompressionResult(
            history=system_msgs + [summary_note] + recent_messages,
            summary_data=summary,
            summary_note=summary_note,
            covered_non_system_count=len(older_messages),
            recent_non_system_count=len(recent_messages),
            used_fallback=False,
        )

    def trim_conversation_history(self, history: List[Dict[str, str]],
                                   keep_recent: int = CONVERSATION_KEEP_RECENT_OVERFLOW) -> List[Dict[str, str]]:
        """裁剪对话历史，只保留最近的消息

        Args:
            history: 原始对话历史
            keep_recent: 保留最近N条消息

        Returns:
            裁剪后的对话历史
        """
        if len(history) <= keep_recent:
            return history

        # 保留系统消息 + 最近N条
        system_msgs = [m for m in history if m.get("role") == "system"]
        non_system = [m for m in history if m.get("role") != "system"]

        trimmed = non_system[-keep_recent:]

        # 在裁剪点添加摘要提示
        summary_note = {
            "role": "system",
            "content": f"[系统] 之前的对话已裁剪（原有 {len(non_system)} 条消息，保留最近 {keep_recent} 条）。如需回顾历史，请查看记忆文件。"
        }

        return system_msgs + [summary_note] + trimmed

    def clear_conversation_history_file(self) -> None:
        """删除对话历史持久化文件"""
        self.provider.clear_conversation_history()


# ===== 兼容旧版的全局单例 API =====

_memory_manager: Optional[MemoryManager] = None


def get_memory_manager(
    base_dir: str = None,
    agent_id: str = None,
    provider: Optional[BaseMemoryProvider] = None,
) -> MemoryManager:
    """获取记忆管理器

    Args:
        base_dir: 基础目录（旧版兼容）
        agent_id: 智能体 ID（新版多智能体模式）

    注意：多智能体模式下不使用单例，每次调用返回新实例
    """
    if agent_id:
        # 多智能体模式：每次创建新实例（每个 agent 有独立路径）
        return MemoryManager(agent_id=agent_id, provider=provider)

    # 兼容旧版：全局单例
    global _memory_manager
    if _memory_manager is None:
        _memory_manager = MemoryManager(base_dir=base_dir, provider=provider)
    return _memory_manager
