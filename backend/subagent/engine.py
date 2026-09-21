"""
子代理引擎 - 极致性能的并行执行引擎

核心优化：
1. 并行启动 - asyncio.gather 实现真正的并行执行
2. 流式响应 - 实时推送进度事件
3. 智能超时 - 分级超时策略，避免卡死
4. 轻量启动 - 预热策略，启动时间 < 100ms
"""

import asyncio
import logging
import time
import uuid
from typing import List, Dict, Any, Optional, AsyncIterator
from dataclasses import dataclass
from enum import Enum

from .templates import SUBAGENT_TEMPLATES, get_subagent_templates

logger = logging.getLogger(__name__)


_SUBAGENT_KIND_LABEL = {
    "subagent_started": "子代理启动",
    "subagent_thinking": "子代理思考中",
    "subagent_tool_call": "子代理调用工具",
    "subagent_tool_result": "子代理工具结果",
    "subagent_completed": "子代理完成",
    "subagent_failed": "子代理失败",
    "subagent_timeout": "子代理超时",
    "subagent_error": "子代理错误",
}

# ─────────────────── 防护配置 ───────────────────

# 同一轮用户消息内 spawn_subagent 最多调用次数（防 LLM 死循环）
MAX_SPAWN_ATTEMPTS_PER_TURN = 1

# 真实子代理并发上限（即使 LLM 给了 N>3 个 agent_types，最多 3 个同时跑）。
# 2026-05-06 从 2 提到 3：默认三专家场景（explore + plan + code-reviewer）应该
# 能并行启动，避免 code-reviewer 一直在 sem 队列里干等。
# 风险：上游 LLM 配额紧时（免费代理）三并发更容易撞 429。如果再次频繁撞限流，
# 调回 2 或改成 os.getenv('LIURUYAN_SUBAGENT_CONCURRENCY', '3')。
SUBAGENT_CONCURRENCY = 3

# 限流错误关键字（识别后端 LLM 限流，向 LLM 反馈"不要立即重试"）
_RATE_LIMIT_MARKERS = (
    "AppIdConcurrencyOverFlowError",
    "AppIdQpsOverFlowError",
    "code: 11202",
    "code: 11203",
    "rate_limit",
    "RateLimit",
    "429",
)


class SubAgentIdleTimeoutError(TimeoutError):
    def __init__(self, stage: str, timeout_s: int):
        super().__init__(f"Subagent idle timeout during {stage} after {timeout_s}s")
        self.stage = stage
        self.timeout_s = timeout_s

# session_id → 已调用次数（每轮 reset）
_session_spawn_attempts: Dict[str, int] = {}

# 全局 lazy semaphore（绑定首次访问的 event loop）
_concurrency_sem: Optional[asyncio.Semaphore] = None


def reset_spawn_attempts(session_id: str) -> None:
    """新一轮用户消息开始时调用，清零计数"""
    _session_spawn_attempts.pop(session_id, None)


def register_spawn_attempt(session_id: str) -> bool:
    """返回 True 表示允许；False 表示已超过本轮上限"""
    cur = _session_spawn_attempts.get(session_id, 0)
    if cur >= MAX_SPAWN_ATTEMPTS_PER_TURN:
        return False
    _session_spawn_attempts[session_id] = cur + 1
    return True


def _get_concurrency_sem() -> asyncio.Semaphore:
    global _concurrency_sem
    if _concurrency_sem is None:
        _concurrency_sem = asyncio.Semaphore(SUBAGENT_CONCURRENCY)
    return _concurrency_sem


def is_rate_limit_error(err_str: str) -> bool:
    return any(marker in err_str for marker in _RATE_LIMIT_MARKERS)


def _output_satisfies_contract(agent_type: str, output: str) -> bool:
    text = (output or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if agent_type == "plan":
        return "critical files for implementation" in lowered or "关键文件" in text
    if agent_type == "code-reviewer":
        review_fields = (
            ("severity:" in lowered or "严重" in text),
            ("location:" in lowered or "位置" in text),
            ("evidence:" in lowered or "证据" in text),
            ("impact:" in lowered or "影响" in text),
            ("suggested fix" in lowered or "修复" in text),
        )
        no_issue_review = (
            ("no actionable issues" in lowered or "未发现" in text or "没有发现" in text)
            and ("overall assessment" in lowered or "总体" in text or "整体" in text)
        )
        return all(review_fields) or no_issue_review
    return True


def _required_output_retry_message(agent_type: str) -> str:
    if agent_type == "plan":
        return (
            "你刚才的回复不符合本 planner 专家的 Required Output 契约。"
            "如果还需要证据，请继续调用只读工具；如果证据已经足够，请直接输出完整计划。"
            "最终回答必须包含 `### Critical Files for Implementation`，并列出 3-5 个关键文件。"
        )
    if agent_type == "code-reviewer":
        return (
            "你刚才的回复不符合本 code-reviewer 专家的 Required Output 契约。"
            "如果还需要证据，请继续调用只读工具；如果证据已经足够，请直接输出完整审查报告。"
            "有问题时每条 finding 必须包含 Severity、Location、Evidence、Impact、Suggested fix direction；"
            "如果没有 actionable issues，也必须说明检查了什么并给出 overall assessment。"
        )
    return "请继续完成任务，并输出符合本专家 Required Output 的最终交付。"


class SubAgentStatus(str, Enum):
    """子代理状态"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"


@dataclass
class SubAgentResult:
    """子代理执行结果"""
    agent_id: str
    agent_type: str
    status: SubAgentStatus
    output: str
    elapsed: float
    tokens_used: int
    tools_called: int
    error: Optional[str] = None


class SubAgentEngine:
    """极致性能的子代理引擎"""

    def __init__(self, session_id: str, agent_id: str, llm_client):
        self.session_id = session_id
        self.agent_id = agent_id
        self.llm_client = llm_client
        # 用合并视图：原生 3 个 + 当前加载的 pack agents（如 webnovel-writer:reviewer）。
        # 每个 SubAgentEngine 实例化都拿最新——pack 装/卸/reload 后下次 spawn 立即可见。
        self.templates = get_subagent_templates()
        self.running_agents: Dict[str, Any] = {}
        # subagent_id → 实时计数（tools_called / tools_completed / thinking_chars）。
        # _call_llm 内部的局部变量在 timeout/cancel 时会丢失，在实例上
        # 镜像一份权威计数让 timeout/failed 事件能拿到“中途到哪了”。
        self._live_counters: Dict[str, Dict[str, int]] = {}

    async def spawn_parallel(
        self,
        agent_types: List[str],
        context: str,
        working_directory: Optional[str] = None,
    ) -> List[SubAgentResult]:
        """
        并行启动多个子代理（核心性能优化）

        性能：比串行快 N 倍（N = 子代理数量）

        Args:
            agent_types: 子代理类型列表
            context: 任务描述（会成为给 LLM 的 user message）
            working_directory: 可选——caller 指定的"目标项目根目录"。设了之后
                每个 subagent 内的 read_file / grep 在 agent_dir 找不到时
                会优先 fallback 到这里（而不是后端进程 cwd）。这是为了支持"用柳如烟
                分析另一个项目"的场景，避免 cwd fallback 错把别的项目源码当自身。
        """

        # 验证子代理类型
        invalid_types = [t for t in agent_types if t not in self.templates]
        if invalid_types:
            raise ValueError(f"Invalid agent types: {invalid_types}")

        # 预生成所有 subagent_id，先发批量启动事件让前端一次性创建占位卡
        prepared = []
        for agent_type in agent_types:
            template = self.templates[agent_type]
            subagent_id = f"subagent_{agent_type}_{uuid.uuid4().hex[:8]}"
            prepared.append((subagent_id, agent_type, template))

        await self._emit_batch_started(prepared)

        # 创建并行任务（携带预生成的 subagent_id）
        tasks = [
            self._spawn_single(
                agent_type, context,
                subagent_id=subagent_id, template=template,
                working_directory=working_directory,
            )
            for subagent_id, agent_type, template in prepared
        ]

        # 并行执行（asyncio.gather）
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 处理异常
        processed_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                processed_results.append(SubAgentResult(
                    agent_id=f"subagent_{i}",
                    agent_type=agent_types[i],
                    status=SubAgentStatus.FAILED,
                    output="",
                    elapsed=0,
                    tokens_used=0,
                    tools_called=0,
                    error=str(result),
                ))
            else:
                processed_results.append(result)

        # 落盘 dump：所有结果（成功/失败/超时）都写到 _docs/subagent_runs/，
        # 让用户事后能完整复盘。失败不影响主流程。
        try:
            from .result_dump import dump_results
            dump_results(
                processed_results, task=context,
                working_directory=working_directory,
            )
        except Exception:
            # 永远不让 dump 失败影响 subagent 返回
            import logging
            logging.getLogger(__name__).debug(
                "[subagent] result_dump failed (non-fatal)", exc_info=True,
            )

        return processed_results

    async def _spawn_single(
        self,
        agent_type: str,
        context: str,
        *,
        subagent_id: str,
        template: Dict[str, Any],
        working_directory: Optional[str] = None,
    ) -> SubAgentResult:
        """启动单个子代理（subagent_started 已由 _emit_batch_started 聚合发出）

        并发保护：通过全局 semaphore 限制真实并行数，避免打爆上游 LLM 限流。

        working_directory 处理：用 ContextVar 注入到本协程上下文，让 file_tools
        的 _resolve_read_path 在解析相对路径时优先 fallback 到这里。token reset
        模式确保 finally 时清理，避免泄漏到其他并发协程。
        """

        start_time = time.time()
        sem = _get_concurrency_sem()

        # 把 working_directory 注入 ContextVar——asyncio task 默认会 copy 父
        # 协程的 context，所以这里 set 的值只影响**本 _spawn_single 协程及其后代**，
        # 不会污染兄弟 subagent。但为了健壮性还是用 token reset 显式清理。
        from backend.tools.builtin.file_tools import _current_working_directory_var
        _wd_token = _current_working_directory_var.set(working_directory)

        try:
            async with sem:
                # 调用 LLM（带超时）
                result = await asyncio.wait_for(
                    self._call_llm(subagent_id, template, context),
                    timeout=template["timeout"]
                )

            elapsed = time.time() - start_time

            output_text = result["output"]
            tools_called_n = result["tools_called"]

            # 空产出守卫：output 空视为失败。
            # 不加这道门槛会让 chunk schema drift 之类的 bug 静默成功（曾经发生过
            # day-1 起 content/thinking/tool_result 字段名不一致，13 秒空跑被标 COMPLETED
            # 整整存活到 2026-05）。orchestrator 看到 success=True 就会误以为分身真完成。
            if not output_text:
                err = (
                    f"子代理 {agent_type} 跑完 {elapsed:.1f}s 但没有最终交付文本 —— "
                    "通常是上游 LLM 返回了空响应、只输出了工具前过程文本、流被中断、或事件 schema 不一致。"
                    "请查后端日志中本 subagent_id 的 [SUBAGENT-] 痕迹定位。"
                )
                await self._emit_event(subagent_id, {
                    "type": "subagent_failed",
                    "error": err,
                    "elapsed": elapsed,
                    "tools_called": tools_called_n,
                    "tools_completed": result.get("tools_completed", 0),
                })
                return SubAgentResult(
                    agent_id=subagent_id,
                    agent_type=agent_type,
                    status=SubAgentStatus.FAILED,
                    output="",
                    elapsed=elapsed,
                    tokens_used=result["tokens_used"],
                    tools_called=tools_called_n,
                    error=err,
                )

            # 发送完成事件（带完整 output，前端 SubAgentCard 终态展开靠它）
            await self._emit_event(subagent_id, {
                "type": "subagent_completed",
                "output": output_text,
                "elapsed": elapsed,
                "tokens_used": result["tokens_used"],
                "tools_called": tools_called_n,
            })

            return SubAgentResult(
                agent_id=subagent_id,
                agent_type=agent_type,
                status=SubAgentStatus.COMPLETED,
                output=output_text,
                elapsed=elapsed,
                tokens_used=result["tokens_used"],
                tools_called=tools_called_n,
            )

        except asyncio.TimeoutError:
            elapsed = time.time() - start_time
            counters = self._live_counters.get(subagent_id, {})
            mid_tools_called = counters.get("tools_called", 0)
            mid_tools_completed = counters.get("tools_completed", 0)
            # 取出中途已收集的产出（_call_llm 内 output_chunks 共享引用）
            partial_chunks = counters.get("_partial_output_chunks") or []
            partial_output = "".join(partial_chunks)
            timeout_output = (
                partial_output + f"\n\n[⚠️ 超时（{template['timeout']}s），以上为中途产出]"
                if partial_output else ""
            )
            await self._emit_event(subagent_id, {
                "type": "subagent_timeout",
                "elapsed": elapsed,
                # 带上中途计数让前端 fallback 文案准确。也带 error 字段所以前端不需 fallback。
                "error": (
                    f"执行超时（{template['timeout']}s）。中途调用了 "
                    f"{mid_tools_completed}/{mid_tools_called} 个工具，"
                    f"已写 {len(partial_output)} 字。"
                    f" 如果任务复杂可考虑在 templates.py 调高 timeout。"
                ),
                "tools_called": mid_tools_called,
                "tools_completed": mid_tools_completed,
                "partial_output_length": len(partial_output),
                "output": timeout_output,
            })
            self._live_counters.pop(subagent_id, None)
            return SubAgentResult(
                agent_id=subagent_id,
                agent_type=agent_type,
                status=SubAgentStatus.TIMEOUT,
                output=timeout_output,
                elapsed=elapsed,
                tokens_used=0,
                tools_called=mid_tools_called,
                error=f"Timeout after {template['timeout']}s",
            )

        except Exception as e:
            elapsed = time.time() - start_time
            counters = self._live_counters.get(subagent_id, {})
            mid_tools_called = counters.get("tools_called", 0)
            mid_tools_completed = counters.get("tools_completed", 0)
            # 同 timeout：异常路径也保留中途产出（rate_limit / 网络抖动等）
            partial_chunks = counters.get("_partial_output_chunks") or []
            partial_output = "".join(partial_chunks)
            failure_output = (
                partial_output + f"\n\n[⚠️ 异常中断：{type(e).__name__}: {e}，以上为中途产出]"
                if partial_output else ""
            )
            await self._emit_event(subagent_id, {
                "type": "subagent_failed",
                "error": str(e),
                "elapsed": elapsed,
                "tools_called": mid_tools_called,
                "tools_completed": mid_tools_completed,
                "partial_output_length": len(partial_output),
                "output": failure_output,
            })
            self._live_counters.pop(subagent_id, None)
            return SubAgentResult(
                agent_id=subagent_id,
                agent_type=agent_type,
                status=SubAgentStatus.FAILED,
                output=failure_output,
                elapsed=elapsed,
                tokens_used=0,
                tools_called=mid_tools_called,
                error=str(e),
            )
        finally:
            # 清理 working_directory ContextVar——避免 token 残留污染后续协程。
            # 每个 _spawn_single 是独立 task（asyncio.gather 起的），各自有 copy 后的
            # context；理论上不 reset 也只影响本协程，但显式清理更稳。
            try:
                _current_working_directory_var.reset(_wd_token)
            except Exception:
                pass

    async def _call_llm(
        self,
        subagent_id: str,
        template: Dict[str, Any],
        context: str,
    ) -> Dict[str, Any]:
        """调用 LLM（带流式进度更新）"""

        from .templates import compute_effective_tools

        # ──────────── 构建 messages（**必须**带 system role，否则 LLM 拿不到角色定位）────────────
        # day-1 起这里只塞 user message，template["system_prompt"] 被包进
        # prompt_cache_info.static 但 LLMClient.chat 的 OpenAI 路径根本不读这字段，
        # Anthropic 路径只在 enable_caching=True 时读 —— 而 subagent 写死 False，
        # 结果：subagent 系统提示词从来没真送达模型。
        # 修法：照主 agent 路径（agent.build_messages）的做法把 system 拼进 messages[0]。
        messages = [
            {"role": "system", "content": template["system_prompt"]},
            {"role": "user", "content": f"{context}\n\n请按照你的专业领域完成分析。"},
        ]

        # ──────────── tool 白名单：tools - disallowed_tools，传给 chat() 的 override ────────────
        # day-1 起这里算了 allowed_tools 但从来没传给 LLMClient.chat()，模型拿到的是
        # 父代理的全部工具集（含 spawn_subagent 等）—— 修法：用 v2 加的
        # allowed_tools_override 参数把模板白名单透传下去。
        # disallowed_tools 优先级高于 tools（防御性：模板写错也不会放行 spawn_*）。
        effective_tools = compute_effective_tools(template)
        agent_type_for_contract = next(
            (name for name, candidate in self.templates.items() if candidate is template),
            "",
        )

        # 调用 LLM（流式）
        tokens_used = 0
        tools_called = 0
        tools_completed = 0
        exploration_chunks = []
        deliverable_chunks = []
        current_thinking = ""

        # ──────────── 硬迭代上限（等价 Claude maxTurns）────────────
        # 模板的 max_tool_rounds 决定 LLM 工具循环最多跑几轮。chain_hook 是
        # LLMClient.chat 现成的 after_model 钩子，每轮返 True 就立刻 break。
        # 比 timeout 精准：模型在第 N 轮被强制收尾给最终答复，而不是被时间刀切断。
        # timeout 仍保留作兜底（防 chain_hook 因 fallback 路径被绕过的极端情况）。
        max_tool_rounds = int(template.get("max_tool_rounds", 8))
        idle_timeout_s = float(template.get("idle_timeout", 45))
        round_counter = {"n": 0}
        cap_fired = {"v": False}
        idle_fired = {"stage": ""}

        async def _round_cap_hook(round_done: Dict[str, Any]) -> bool:
            if not round_done.get("has_tool_calls"):
                return False
            round_counter["n"] += 1
            if round_counter["n"] >= max_tool_rounds:
                cap_fired["v"] = True
                # engine 全局用 print + [SUBAGENT-XXX] tag 风格记日志，跟 _emit_event 保持一致
                logger.warning("[SUBAGENT-CAP] %s reached maxTurns=%s", subagent_id, max_tool_rounds)
                return True
            return False

        async def _iter_chunks_with_idle(
            source: AsyncIterator[Dict[str, Any]],
            stage: str,
            timeout_s: float = idle_timeout_s,
        ):
            iterator = source.__aiter__()
            seen_first_chunk = False
            previous_chunk_type = ""
            while True:
                try:
                    if seen_first_chunk and previous_chunk_type != "tool_call":
                        chunk = await asyncio.wait_for(iterator.__anext__(), timeout=timeout_s)
                    else:
                        chunk = await iterator.__anext__()
                        seen_first_chunk = True
                    previous_chunk_type = chunk.get("type", "")
                    yield chunk
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError as exc:
                    idle_fired["stage"] = stage
                    logger.warning(
                        "[SUBAGENT-IDLE] %s stage=%s idle_timeout=%ss",
                        subagent_id, stage, timeout_s,
                    )
                    await self._emit_event(subagent_id, {
                        "type": "subagent_progress",
                        "stage": "idle_timeout",
                        "note": f"{stage} 阶段 {timeout_s}s 无新事件，提前收束",
                    })
                    raise SubAgentIdleTimeoutError(stage, int(timeout_s)) from exc

        # 初始化实例级计数，让 timeout/failed 能拿到中途到哪了
        # content_chars: 给非 thinking 模型（gpt/glm/astron 等）一个进度信号；
        # 否则 UI 上"字符增长"长期 0，看着像卡死
        # _partial_output_chunks: 共享 exploration_chunks list 引用——timeout / 异常路径
        # 能从这里取出已经收集的产出，不再返 output=""（线上事故 2026-05-06：
        # 探索专家 timeout 180s 中途其实写了不少东西，但 output 全丢）
        self._live_counters[subagent_id] = {
            "tools_called": 0,
            "tools_completed": 0,
            "thinking_chars": 0,
            "content_chars": 0,
            "_partial_output_chunks": exploration_chunks,
        }

        try:
            # ──────────────── chunk schema 契约（必须跟 LLMClient.chat 对齐）────────────────
            # 来源：backend/llm_client.py 主路径 / bare 路径都 yield 这套：
            #   {"type": "content",     "text":  ...}
            #   {"type": "thinking",    "text":  ...}
            #   {"type": "tool_call",   "name", "input",  "tool_use_id"}
            #   {"type": "tool_result", "name", "result", "tool_use_id"}
            #   {"type": "system_notice", "kind": "invalid_tool_arguments", ...}
            # 注意：llm_client.chat 不 yield "usage" —— token 计数走 _chat_one_round 内部
            # round_done.final_usage，跨层捞代价大，本层 tokens_used 暂保 0（TODO）。
            # 任何字段名错位都会让 subagent 静默空跑然后被标 COMPLETED ——
            # 改这块前先核对 llm_client.chat 的 docstring（line ~1294）和实际 yield。
            # ──────────────────────────────────────────────────────────────────────────────
            # 进度节流：thinking / content 各每涨 200 字符 emit 一次 progress 事件
            # content 通道是给非 thinking 模型（gpt/glm/astron）一个进度信号——
            # 否则前端 thinkingChars 长期 0，UI 上看着像卡死
            last_thinking_emit_len = 0
            last_content_emit_len = 0
            THINKING_EMIT_STEP = 200
            CONTENT_EMIT_STEP = 200

            exploration_stream = self.llm_client.chat(
                messages=messages,
                stream=True,
                use_tools=True,
                agent_id=self.agent_id,
                session_id=self.session_id,
                prompt_cache_info={
                    "enable_caching": False,
                    "static": template["system_prompt"],
                    "dynamic": "",
                },
                allowed_tools_override=effective_tools,
                chain_hook=_round_cap_hook,
            )
            async for chunk in _iter_chunks_with_idle(exploration_stream, "exploration"):
                chunk_type = chunk.get("type", "")

                if chunk_type == "content":
                    text = chunk.get("text", "")
                    if text:
                        exploration_chunks.append(text)
                        # 一旦有工具调用就进 deliverable_chunks（不依赖 tools_completed）。
                        # 根因：tools_completed 只在 tool_result chunk 时才 > 0；
                        # 如果 idle timeout 发生在工具执行期间（45s 无 chunk），
                        # 原逻辑让这段 content 丢失导致 output=""（2026-05-06 bug）。
                        if tools_called > 0:
                            deliverable_chunks.append(text)
                        # 给前端 progress 一个真实进度信号（非 thinking 模型 thinking_chars
                        # 永远是 0，没这个 UI 看起来就像卡死了）
                        total_content = sum(len(c) for c in exploration_chunks)
                        self._live_counters[subagent_id]["content_chars"] = total_content

                        # 节流 emit：每涨 200 字符发一次 subagent_content 事件
                        # 跟 subagent_thinking 对称，前端 SubAgentCard 用同一套 progress 渲染
                        if total_content - last_content_emit_len >= CONTENT_EMIT_STEP:
                            last_content_emit_len = total_content
                            await self._emit_event(subagent_id, {
                                "type": "subagent_content",
                                "content": text[-100:],  # 末 100 字给前端尾部预览
                                "total_length": total_content,
                            })

                elif chunk_type == "thinking":
                    # reasoning / 思考流：累入 thinking 计数；不并入 output（output
                    # 是给主 agent 看的最终交付，思考过程只走前端进度展示）
                    text = chunk.get("text", "")
                    if not text:
                        continue
                    current_thinking += text
                    self._live_counters[subagent_id]["thinking_chars"] = len(current_thinking)

                    # 节流：thinking 每涨 200 字符 emit 一次（不是 % 取模，避免跳帧）
                    if len(current_thinking) - last_thinking_emit_len >= THINKING_EMIT_STEP:
                        last_thinking_emit_len = len(current_thinking)
                        await self._emit_event(subagent_id, {
                            "type": "subagent_thinking",
                            "content": current_thinking[-100:],  # 最后 100 字符给前端做尾部预览
                            "total_length": len(current_thinking),
                        })

                elif chunk_type == "tool_call":
                    tools_called += 1
                    deliverable_chunks.clear()
                    self._live_counters[subagent_id]["tools_called"] = tools_called
                    await self._emit_event(subagent_id, {
                        "type": "subagent_tool_call",
                        "name": chunk.get("name", ""),
                        "input": chunk.get("input", {}),
                        "tools_called": tools_called,
                    })

                elif chunk_type == "tool_result":
                    # llm_client 字段名是 "result" 不是 "output" —— day-1 起这里写错过
                    tools_completed += 1
                    self._live_counters[subagent_id]["tools_completed"] = tools_completed
                    raw_result = chunk.get("result", "")
                    await self._emit_event(subagent_id, {
                        "type": "subagent_tool_result",
                        "name": chunk.get("name", ""),
                        "output": str(raw_result)[:200],  # 截断长输出，前端只做摘要
                    })

                # 其它事件类型（system_notice 等）当前不消费

        except SubAgentIdleTimeoutError:
            if tools_completed == 0:
                raise
        except Exception as e:
            # 记录错误但不中断
            await self._emit_event(subagent_id, {
                "type": "subagent_error",
                "error": str(e),
            })
            raise

        requires_tool_use = bool(template.get("requires_tool_use"))
        if requires_tool_use and tools_called == 0 and effective_tools:
            logger.warning(
                "[SUBAGENT-RETRY] %s produced no tool calls in "
                "required-tool exploration phase, retrying once",
                subagent_id,
            )
            await self._emit_event(subagent_id, {
                "type": "subagent_progress",
                "stage": "retrying_tool_exploration",
                "note": "首次探索未调用工具，正在重试并要求先读取项目文件",
            })
            retry_messages = list(messages) + [
                {"role": "assistant", "content": "".join(exploration_chunks)},
                {"role": "user", "content": (
                    "你刚才只描述了准备探索，但没有调用任何工具。"
                    "本专家任务必须基于真实项目文件完成。"
                    "请现在先调用可用的文件发现或读取工具（例如 glob_search / glob_by_name / read_file），"
                    "不要直接输出最终报告。"
                )},
            ]
            exploration_chunks.clear()
            self._live_counters[subagent_id]["content_chars"] = 0
            last_content_emit_len = 0
            try:
                retry_stream = self.llm_client.chat(
                    messages=retry_messages,
                    stream=True,
                    use_tools=True,
                    agent_id=self.agent_id,
                    session_id=self.session_id,
                    prompt_cache_info={
                        "enable_caching": False,
                        "static": template["system_prompt"],
                        "dynamic": "",
                    },
                    allowed_tools_override=effective_tools,
                    chain_hook=_round_cap_hook,
                )
                async for chunk in _iter_chunks_with_idle(retry_stream, "retry_exploration"):
                    chunk_type = chunk.get("type", "")

                    if chunk_type == "content":
                        text = chunk.get("text", "")
                        if text:
                            exploration_chunks.append(text)
                            # 一旦有工具调用就进 deliverable_chunks（不依赖 tools_completed）。
                            # 根因：tools_completed 只在 tool_result 时才 > 0；
                            # idle timeout 发生在工具执行期间时这段 content 会丢失导致 output=""
                            if tools_called > 0:
                                deliverable_chunks.append(text)
                            total_content = sum(len(c) for c in exploration_chunks)
                            self._live_counters[subagent_id]["content_chars"] = total_content
                            if total_content - last_content_emit_len >= CONTENT_EMIT_STEP:
                                last_content_emit_len = total_content
                                await self._emit_event(subagent_id, {
                                    "type": "subagent_content",
                                    "content": text[-100:],
                                    "total_length": total_content,
                                })
                    elif chunk_type == "thinking":
                        text = chunk.get("text", "")
                        if not text:
                            continue
                        current_thinking += text
                        self._live_counters[subagent_id]["thinking_chars"] = len(current_thinking)
                        if len(current_thinking) - last_thinking_emit_len >= THINKING_EMIT_STEP:
                            last_thinking_emit_len = len(current_thinking)
                            await self._emit_event(subagent_id, {
                                "type": "subagent_thinking",
                                "content": current_thinking[-100:],
                                "total_length": len(current_thinking),
                            })
                    elif chunk_type == "tool_call":
                        tools_called += 1
                        deliverable_chunks.clear()
                        self._live_counters[subagent_id]["tools_called"] = tools_called
                        await self._emit_event(subagent_id, {
                            "type": "subagent_tool_call",
                            "name": chunk.get("name", ""),
                            "input": chunk.get("input", {}),
                            "tools_called": tools_called,
                        })
                    elif chunk_type == "tool_result":
                        tools_completed += 1
                        self._live_counters[subagent_id]["tools_completed"] = tools_completed
                        raw_result = chunk.get("result", "")
                        await self._emit_event(subagent_id, {
                            "type": "subagent_tool_result",
                            "name": chunk.get("name", ""),
                            "output": str(raw_result)[:200],
                        })
                messages = retry_messages
            except SubAgentIdleTimeoutError:
                if tools_completed == 0:
                    raise
                messages = retry_messages
            except Exception as e:
                await self._emit_event(subagent_id, {
                    "type": "subagent_error",
                    "error": str(e),
                })
                raise

        output_before_contract_retry = (
            "".join(deliverable_chunks) if tools_called > 0 else "".join(exploration_chunks)
        )
        if (
            requires_tool_use
            and tools_called > 0
            and effective_tools
            and not _output_satisfies_contract(agent_type_for_contract, output_before_contract_retry)
        ):
            if not _output_satisfies_contract(agent_type_for_contract, output_before_contract_retry):
                logger.warning(
                    "[SUBAGENT-RETRY] %s output violates Required Output contract, retrying once",
                    subagent_id,
                )
                await self._emit_event(subagent_id, {
                    "type": "subagent_progress",
                    "stage": "retrying_required_output",
                    "note": "模型输出不符合专家交付契约，正在要求继续调用工具或输出合格最终报告",
                })
                retry_messages = list(messages) + [
                    {"role": "assistant", "content": output_before_contract_retry},
                    {"role": "user", "content": _required_output_retry_message(agent_type_for_contract)},
                ]
                deliverable_chunks.clear()
                try:
                    retry_stream = self.llm_client.chat(
                        messages=retry_messages,
                        stream=True,
                        use_tools=True,
                        agent_id=self.agent_id,
                        session_id=self.session_id,
                        prompt_cache_info={
                            "enable_caching": False,
                            "static": template["system_prompt"],
                            "dynamic": "",
                        },
                        allowed_tools_override=effective_tools,
                        chain_hook=_round_cap_hook,
                    )
                    async for chunk in _iter_chunks_with_idle(retry_stream, "retry_required_output"):
                        chunk_type = chunk.get("type", "")
                        if chunk_type == "content":
                            text = chunk.get("text", "")
                            if text:
                                exploration_chunks.append(text)
                                if tools_called > 0:
                                    deliverable_chunks.append(text)
                                total_content = sum(len(c) for c in exploration_chunks)
                                self._live_counters[subagent_id]["content_chars"] = total_content
                                if total_content - last_content_emit_len >= CONTENT_EMIT_STEP:
                                    last_content_emit_len = total_content
                                    await self._emit_event(subagent_id, {
                                        "type": "subagent_content",
                                        "content": text[-100:],
                                        "total_length": total_content,
                                    })
                        elif chunk_type == "thinking":
                            text = chunk.get("text", "")
                            if not text:
                                continue
                            current_thinking += text
                            self._live_counters[subagent_id]["thinking_chars"] = len(current_thinking)
                            if len(current_thinking) - last_thinking_emit_len >= THINKING_EMIT_STEP:
                                last_thinking_emit_len = len(current_thinking)
                                await self._emit_event(subagent_id, {
                                    "type": "subagent_thinking",
                                    "content": current_thinking[-100:],
                                    "total_length": len(current_thinking),
                                })
                        elif chunk_type == "tool_call":
                            tools_called += 1
                            deliverable_chunks.clear()
                            self._live_counters[subagent_id]["tools_called"] = tools_called
                            await self._emit_event(subagent_id, {
                                "type": "subagent_tool_call",
                                "name": chunk.get("name", ""),
                                "input": chunk.get("input", {}),
                                "tools_called": tools_called,
                            })
                        elif chunk_type == "tool_result":
                            tools_completed += 1
                            self._live_counters[subagent_id]["tools_completed"] = tools_completed
                            raw_result = chunk.get("result", "")
                            await self._emit_event(subagent_id, {
                                "type": "subagent_tool_result",
                                "name": chunk.get("name", ""),
                                "output": str(raw_result)[:200],
                            })
                    messages = retry_messages
                except SubAgentIdleTimeoutError:
                    if tools_completed == 0:
                        raise
                    messages = retry_messages
                except Exception as e:
                    await self._emit_event(subagent_id, {
                        "type": "subagent_error",
                        "error": str(e),
                    })
                    raise

        # 成功路径清理 live_counters（timeout/failed 路径在调用者那里清）
        self._live_counters.pop(subagent_id, None)
        output = "".join(deliverable_chunks) if tools_called > 0 else "".join(exploration_chunks)
        if requires_tool_use and tools_called > 0 and not _output_satisfies_contract(agent_type_for_contract, output):
            output = ""

        return {
            "output": output,
            "tokens_used": tokens_used,
            "tools_called": tools_called,
            "tools_completed": tools_completed,
        }

    async def _emit_event(self, subagent_id: str, event: Dict[str, Any]):
        """发送事件到前端（通过 SSE 总线，按 session_id 路由）

        事件形态（包装为主 SSE 识别的 system_notice 结构）：
            {
              "type": "system_notice",
              "kind": "subagent_thinking" / "subagent_tool_call" / ...,
              "payload": { subagent_id, agent_type, ...原事件字段 },
              "session_id": self.session_id
            }
        前端 runtimeBlocks 识别 system_notice + kind 前缀 subagent_*，按 payload.subagent_id
        分发到对应卡片。
        """
        from backend.sse_manager import get_sse_manager

        # 原事件的 type 作为 system_notice.kind
        kind = event.pop("type", "subagent_event")
        payload = {
            "subagent_id": subagent_id,
            **event,
        }
        # content 为可读摘要（build_runtime_transport_event 要求 system_notice 必须有 content）
        content = _SUBAGENT_KIND_LABEL.get(kind, kind)
        transport = {
            "type": "system_notice",
            "kind": kind,
            "content": content,
            "payload": payload,
            "session_id": self.session_id,
        }

        # 发送到 SSE 总线（按 session_id 路由，主 SSE 端点合流透传）
        sse_manager = get_sse_manager()
        await sse_manager.send_event(
            session_id=self.session_id,
            event=transport,
        )

    async def _emit_batch_started(self, prepared: List[tuple]) -> None:
        """发送批量启动事件，让前端一次性渲染所有占位卡

        Args:
            prepared: [(subagent_id, agent_type, template), ...]
        """
        from backend.sse_manager import get_sse_manager

        subagents_meta = [
            {
                "subagent_id": sid,
                "agent_type": atype,
                "icon": tpl["icon"],
                "name": tpl["name"],
            }
            for sid, atype, tpl in prepared
        ]
        transport = {
            "type": "system_notice",
            "kind": "subagents_batch_started",
            "content": f"启动 {len(subagents_meta)} 个子代理",
            "payload": {
                "subagents": subagents_meta,
            },
            "session_id": self.session_id,
        }
        sse_manager = get_sse_manager()
        await sse_manager.send_event(
            session_id=self.session_id,
            event=transport,
        )
