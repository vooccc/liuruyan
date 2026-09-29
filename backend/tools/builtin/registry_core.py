"""工具注册表引擎核心（从 clean_registry.py 抽出）。

包含 CleanToolRegistry（register / dispatch_batch / execute / 工具定义 / stats）、
并发类常量、结果预算 _BUDGET、read-before-edit gate 与 dispatch 展示 helper。
行为与原 clean_registry 完全一致；clean_registry 再导入这些符号保持公共 API，
并保留全局 singleton `registry` 与各域 registrations。

独立 import 安全：顶部 load_dotenv 一次（_BUDGET.from_env 读预算 env，
与 clean_registry 原顶部 load_dotenv 意图一致）。
"""
import asyncio
import inspect
import json
import logging
import time
import uuid
from typing import Any, Callable, Dict, Iterable, List, Optional

try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:
    pass

from .registry_validation import (
    InputValidationError,
    _coerce_arguments_by_schema,
    _normalize_aliases_and_validate,
)

logger = logging.getLogger(__name__)

# ============================================================================
# 并发类（concurrency class）—— 调度层判定一批 tool_calls 是否能并行
# ============================================================================
#
# - "read"      纯读，无副作用 → 同批多个可 asyncio.gather 并发
# - "write"     有写文件 / 改持久状态副作用 → 串行；不能与同批写并发（顺序敏感）
# - "exclusive" 长阻塞 / 与外部强交互（ask_user_question、execute_command 等）→ 必须串行且独占
#
# dispatch_batch 把一批 tool_calls 切成"连续 read 段 + 单个 write/exclusive"分组：
# - 连续多个 read 工具 → 一次 gather 并发
# - 任何 write 或 exclusive → 单独 await，前后 flush 并发段
# 结果按原顺序回填 results_by_id，调用方按原 for 循环读，事件顺序与之前一致。
CONCURRENCY_READ = "read"
CONCURRENCY_WRITE = "write"
CONCURRENCY_EXCLUSIVE = "exclusive"

_DEFAULT_CONCURRENCY_CLASSES: Dict[str, str] = {
    # 读类：文件 / 搜索 / 诊断 / 历史 / 网络 / 内存查询 / 任务查询 / 反思
    "read_file": CONCURRENCY_READ,
    "grep": CONCURRENCY_READ,
    "glob_search": CONCURRENCY_READ,
    "outline_file": CONCURRENCY_READ,
    "git_status": CONCURRENCY_READ,
    "git_log": CONCURRENCY_READ,
    "git_diff": CONCURRENCY_READ,
    "git_blame": CONCURRENCY_READ,
    "get_diagnostics": CONCURRENCY_READ,
    "check_typescript": CONCURRENCY_READ,
    "code_intelligence": CONCURRENCY_READ,  # jedi 静态分析，无副作用
    "web_search": CONCURRENCY_READ,
    "web_extract": CONCURRENCY_READ,
    "browser_preview": CONCURRENCY_READ,  # 仅做 URL 校验 + 返结构化 JSON，无副作用
    "read_logs": CONCURRENCY_READ,  # 只读日志文件，tail/grep/level filter
    "read_frontend_events": CONCURRENCY_READ,  # 前端遥测语义化读取（路径 A 配套）
    "session_search": CONCURRENCY_READ,  # 只读扫盘 transcript.jsonl，无副作用
    "recall_memory": CONCURRENCY_READ,
    "task_get": CONCURRENCY_READ,
    "task_list": CONCURRENCY_READ,
    "tool_self_reflect": CONCURRENCY_READ,
    # 后台命令状态查询（拉快照，无副作用——kill 是显式参数，调用方自己控制语义）
    "command_status": CONCURRENCY_READ,
    # 写类：磁盘 / 持久状态突变
    "write_file": CONCURRENCY_WRITE,
    "edit": CONCURRENCY_WRITE,
    "multi_edit": CONCURRENCY_WRITE,
    "notebook_edit": CONCURRENCY_WRITE,
    "create_memory": CONCURRENCY_WRITE,
    "memory_audit": CONCURRENCY_WRITE,  # 可能写盘删记忆/合并；带 dry-run 时实质是 read 但保守归类 write
    "task_create": CONCURRENCY_WRITE,
    "task_update": CONCURRENCY_WRITE,
    # 独占：长阻塞 / 与外部强交互
    "execute_command": CONCURRENCY_EXCLUSIVE,
    "execute_powershell": CONCURRENCY_EXCLUSIVE,
    "ask_user_question": CONCURRENCY_EXCLUSIVE,
    "spawn_subagent": CONCURRENCY_EXCLUSIVE,
    # Cron / Sleep（FEATURE_CRON_TOOL 门控）
    "cron_create": CONCURRENCY_WRITE,    # 写 cron_jobs.json
    "cron_list": CONCURRENCY_READ,       # 仅查内存 + 计算 next_fire_at
    "cron_delete": CONCURRENCY_WRITE,    # 写 cron_jobs.json
    "cron_set_enabled": CONCURRENCY_WRITE,  # 写 cron_jobs.json
    "sleep": CONCURRENCY_EXCLUSIVE,      # 长阻塞 await asyncio.sleep
    # Skill Pack 管理（兼容 Anthropic Claude Code Plugin 协议）
    "install_skill_pack": CONCURRENCY_EXCLUSIVE,   # git clone + pip install，分钟级
    "update_skill_pack": CONCURRENCY_EXCLUSIVE,    # git pull + pip install
    "uninstall_skill_pack": CONCURRENCY_WRITE,     # 删目录
    "list_skill_packs": CONCURRENCY_READ,
    "skill_pack_status": CONCURRENCY_READ,
}


class CleanToolRegistry:
    """清理后的工具注册表"""

    def __init__(self):
        self.tools: Dict[str, Dict[str, Any]] = {}

    def register(
        self,
        name: str,
        description: str,
        input_schema: Dict[str, Any],
        handler: Callable,
        arg_aliases: Optional[Dict[str, str]] = None,
        **kwargs
    ):
        """注册工具

        Args:
            arg_aliases: 别名表（如 {"directory": "path", "files": "paths"}）。
                LLM 常把某些参数名交换写（各家模型“母语”不同），dispatch 前
                会按这个表把 alias 转为标准 key。不在表中、也不在 schema.properties
                中的字段会被 strict 门 拒。默认 None。
            kwargs: 可含 concurrency_class 显式覆盖默认映射；未声明时按
                _DEFAULT_CONCURRENCY_CLASSES 兜底；新工具默认 exclusive。
        """
        if "concurrency_class" not in kwargs:
            kwargs["concurrency_class"] = _DEFAULT_CONCURRENCY_CLASSES.get(
                name, CONCURRENCY_EXCLUSIVE
            )

        # 对齐 Claude 的 z.strictObject：schema 未显式设置时默认 additionalProperties=False。
        # 部分 OpenAI 兼容 provider 启用 strict mode 后会服务端拒未知字段；
        # 其他 provider 不响应也不会看负（registry 侧 strict 门仍会拦）。
        if isinstance(input_schema, dict) and input_schema.get("type") == "object":
            input_schema.setdefault("additionalProperties", False)

        self.tools[name] = {
            "name": name,
            "description": description,
            "input_schema": input_schema,
            "handler": handler,
            "arg_aliases": arg_aliases or {},
            **kwargs
        }
        alias_n = len(arg_aliases) if arg_aliases else 0
        logger.info(
            f"Registered tool: {name} (concurrency={kwargs['concurrency_class']}, aliases={alias_n})"
        )

    def get_concurrency_class(self, name: str) -> str:
        """查工具的并发类；未注册的返回 exclusive。"""
        tool = self.tools.get(name)
        if not tool:
            return CONCURRENCY_EXCLUSIVE
        return tool.get("concurrency_class", CONCURRENCY_EXCLUSIVE)

    async def dispatch_batch(
        self,
        parsed_tool_calls: List[Dict[str, Any]],
        **execute_kwargs,
    ) -> Dict[str, Dict[str, Any]]:
        """批量执行一组 tool_calls，对连续 read 段做并发 gather。

        Args:
            parsed_tool_calls: 形如 [{"id": str, "name": str, "arguments": dict,
                                      "_json_repair_layers": list?}, ...]
            **execute_kwargs: 透传给 self.execute()（agent_id / session_id /
                              llm_client 等）

        Returns:
            {tc_id: {"result": str}} —— execute() 已吞异常为 "Error: ..." 字符串，
            所以正常路径不会有 exception 字段；保留 dict 形态便于未来扩展（比如
            想区分 raw exception 时直接加 {"exception": e}）。

        排序保证：返回 dict 包含所有 tc_id；调用方按原 parsed_tool_calls 顺序
        for 遍历查表即可，事件、messages 配对、spawn_dispatched 判定均不变。

        关键设计：
        - 连续 read：一次 asyncio.gather 并发执行
        - write：单独 await，flush 当前并发段
        - exclusive：单独 await（同 write，但语义上更强：它们通常会改变后续工具
          能看到的世界状态——execute_command 可能动磁盘）
        """
        if not parsed_tool_calls:
            return {}

        results: Dict[str, Dict[str, Any]] = {}
        pending_reads: List[Dict[str, Any]] = []

        # v2 chain hook 接入点（C1'）：from_context 拿当前 run 的 pre/post hook。
        # 没有 hook 时（v1 主路径 / chain shim flag OFF）行为完全不变。
        # backend → harness 是 boundary CI 允许方向（红线只禁反向）。
        #
        # 关键：必须用短路径 `liuruyan.*`（跟 main.py / 测试一致），不要用
        # `backend.packages.harness.liuruyan.*`——backend/__init__.py 已把
        # harness 路径加进 sys.path，两条路径会让同一文件被加载成**两个独立
        # module**，ContextVar 实例不通（这条规则适用所有跨 backend ↔ harness
        # 的 import：永远 `liuruyan.xxx`）。
        try:
            from liuruyan.runtime.context import (
                get_post_tool_hook,
                get_pre_tool_hook,
            )
            _pre_hook = get_pre_tool_hook()
            _post_hook = get_post_tool_hook()
        except Exception:
            # harness 不可用（极罕见，比如打包错配）→ 完全降级到 v1 行为
            _pre_hook = None
            _post_hook = None

        async def _run_one(tc: Dict[str, Any]):
            tc_id = tc.get("id") or ""
            # trace_id 优先用 LLM 给的 tool_call.id（Anthropic native: tool_use_id；
            # OpenAI compat: tool_calls[].id），缺失才让 execute() 内部生成 uuid 兜底。
            # 这样 SSE 事件里 yield 出去的 trace_id 跟 stats 行里的能对得上。
            trace_id = tc_id or None

            # ── pre hook（v2 chain 注入点）─────────────────────────────────
            effective_args = tc["arguments"]
            short_circuited = False
            short_result: Any = None
            if _pre_hook is not None:
                try:
                    pre_ret = await _pre_hook(
                        tc["name"], effective_args, tc_id or ""
                    )
                except Exception:
                    logger.exception(
                        "dispatch_batch pre_tool_hook swallowed exception (tool=%s)",
                        tc.get("name"),
                    )
                    pre_ret = None
                if isinstance(pre_ret, dict):
                    if "short_circuit" in pre_ret:
                        # mw 11 GuardrailChain 命中：跳过工具执行，用 hook 给的值当 result
                        short_circuited = True
                        short_result = pre_ret["short_circuit"]
                    elif "replaced_args" in pre_ret and isinstance(
                        pre_ret["replaced_args"], dict
                    ):
                        # mw 改写 args（如虚拟路径替换）— 用新 args 继续执行
                        effective_args = pre_ret["replaced_args"]

            # ── 真执行（除非 short_circuited）─────────────────────────────
            if short_circuited:
                r = short_result
                tool_exception: Optional[BaseException] = None
            else:
                try:
                    r = await self.execute(
                        tc["name"],
                        effective_args,
                        json_repair_layers=tc.get("_json_repair_layers", []),
                        trace_id=trace_id,
                        **execute_kwargs,
                    )
                    tool_exception = None
                except Exception as exc:
                    # execute() 内部已吞异常成字符串；这里只是兜底，避免 gather 整批挂掉
                    logger.exception(
                        f"dispatch_batch _run_one unexpected exception for {tc.get('name')}"
                    )
                    r = None
                    tool_exception = exc

            # ── post hook（v2 chain 注入点）────────────────────────────────
            if _post_hook is not None and tool_exception is None:
                # 异常路径不调 post hook（让上层兜底逻辑保持原样）
                try:
                    is_error = isinstance(r, str) and r.startswith("Error:")
                    post_ret = await _post_hook(
                        tc["name"], effective_args, tc_id or "", r, is_error
                    )
                except Exception:
                    logger.exception(
                        "dispatch_batch post_tool_hook swallowed exception (tool=%s)",
                        tc.get("name"),
                    )
                    post_ret = None
                if isinstance(post_ret, dict) and "replaced_result" in post_ret:
                    # mw 13 ToolErrorRecovery 归一化等场景
                    r = post_ret["replaced_result"]

            if tool_exception is not None:
                return tc_id, {
                    "exception": tool_exception,
                    "trace_id": trace_id or tc_id,
                }
            return tc_id, {"result": r, "trace_id": trace_id or tc_id}

        async def _flush_reads():
            nonlocal pending_reads
            if not pending_reads:
                return
            if len(pending_reads) == 1:
                # 单个 read 不必 gather，直接 await（避免 task 调度开销）
                tc_id, res = await _run_one(pending_reads[0])
                results[tc_id] = res
            else:
                gathered = await asyncio.gather(
                    *[_run_one(tc) for tc in pending_reads],
                    return_exceptions=False,  # _run_one 已 try/except 内部化
                )
                for tc_id, res in gathered:
                    results[tc_id] = res
            pending_reads = []

        for tc in parsed_tool_calls:
            cls = self.get_concurrency_class(tc["name"])
            if cls == CONCURRENCY_READ:
                pending_reads.append(tc)
            else:
                # write / exclusive：先把前面的 read 段并发执行掉，再串行跑当前
                await _flush_reads()
                tc_id, res = await _run_one(tc)
                results[tc_id] = res

        await _flush_reads()
        return results

    def get_tool_definitions(
        self,
        format: str = "anthropic",
        allowed_tools: Optional[Iterable[str]] = None,
    ) -> List[Dict[str, Any]]:
        """获取工具定义。

        Args:
            format: 'anthropic' 或 'openai'
            allowed_tools: 白名单——未传/None 时不过滤（向后兼容，全部工具都暴露）；
                传 list/set 时仅返回名字在其中的工具 schema。
                设计目的：每个 agent 只需要"自己用得到"的工具进 system prompt，
                70+ 工具的 schema 累计可达 1 万+ token，按 agent 裁剪后能砍 60-90% prompt 成本。
                未匹配的名字静默忽略——白名单字段写错时降级到"少数工具可用"而不是"全 agent 没工具"。
        """
        if allowed_tools is not None:
            allowed_set = {str(name) for name in allowed_tools}
        else:
            allowed_set = None

        definitions = []
        for tool in self.tools.values():
            if allowed_set is not None and tool["name"] not in allowed_set:
                continue
            if format == "openai":
                # OpenAI 格式：需要 type 和 function 字段
                definitions.append({
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool["description"],
                        "parameters": tool["input_schema"]
                    }
                })
            else:
                # Anthropic 格式（默认）
                definitions.append({
                    "name": tool["name"],
                    "description": tool["description"],
                    "input_schema": tool["input_schema"]
                })
        return definitions

    async def _dispatch(self, name: str, arguments: Dict[str, Any], context: Dict[str, Any] = None) -> str:
        """无异常吞没的内部分发。成功返回字符串；工具不存在 → KeyError；handler 异常直接上抛。

        供 execute() 使用，让成功率判定只认异常而不靠字符串前缀。
        """
        if name not in self.tools:
            raise KeyError(f"Tool '{name}' not found")

        tool = self.tools[name]
        handler = tool["handler"]
        is_async = tool.get("is_async", False)

        # ── 调度层统一参数 coerce ─────────────────────────────────────────────
        # OpenAI 兼容协议（讯飞 / DeepSeek / chatgpt5.x）的 tool_call args 反序列化
        # 后偶尔会出现：integer/number 来成 str（"1" 而非 1）、boolean 来成 "true" 等。
        # 不在调度层抹平，每个工具都得自己 int() 防御（read_file 早期就因此卡死过 worker）。
        # 按 input_schema.properties[].type 做 best-effort 转换，失败时不改原值——
        # 让 handler 自己拿到原值报错，调度层不该掩盖语义错误。
        # 对齐 Claude z.strictObject：先别名归一化，再 strict 校验 keys。
        # 任何不在 schema.properties 且不是别名、不是 context 注入键的
        # 参数 → InputValidationError，不走到 handler。
        validated_arguments = _normalize_aliases_and_validate(
            name, arguments, tool.get("input_schema"), tool.get("arg_aliases"),
        )
        coerced_arguments = _coerce_arguments_by_schema(validated_arguments, tool.get("input_schema"))

        all_args = {**coerced_arguments}
        if context:
            signature = inspect.signature(handler)
            accepts_var_kwargs = any(
                param.kind == inspect.Parameter.VAR_KEYWORD
                for param in signature.parameters.values()
            )
            if accepts_var_kwargs:
                all_args.update(context)
            else:
                accepted_names = set(signature.parameters.keys())
                for key, value in context.items():
                    if key in accepted_names:
                        all_args[key] = value

        if is_async:
            result = await handler(**all_args)
        else:
            result = handler(**all_args)

        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False)

    async def execute_tool(self, name: str, arguments: Dict[str, Any], context: Dict[str, Any] = None) -> str:
        """执行工具（兼容入口：吞异常为 'Error: ...' 字符串）。

        新代码请用 execute()。本方法保留是为了向后兼容直接拿 registry.execute_tool 的调用点。
        """
        try:
            return await self._dispatch(name, arguments, context)
        except KeyError as e:
            return f"Error: {str(e).strip(chr(39))}"  # strip single-quotes from KeyError
        except InputValidationError as e:
            return f"Error: {str(e)}"
        except Exception as e:
            logger.exception(f"Error executing tool {name}")
            return f"Error: {str(e)}"

    async def execute(self, name: str, arguments: Dict[str, Any], **kwargs) -> str:
        """执行工具（兼容旧接口，支持统计收集）

        额外参数：
        - agent_id: 代理 ID
        - session_id: 会话 ID
        - llm_client: LLM 客户端实例
        - json_repair_layers: JSON 修复层信息（用于统计）
        - trace_id: 单次 tool_call 唯一 id；调用方（dispatch_batch / llm_client）传 tc["id"]
          串起 SSE / messages / audit / stats 全链。未传时本方法生成 'tc-' 前缀 uuid12 兜底，
          保证 stats 行永远有非空 trace_id 可以用来跨日志关联。
        """
        import time
        import uuid as _uuid
        start_time = time.time()
        json_repair_layers = kwargs.get('json_repair_layers', [])
        agent_id = kwargs.get('agent_id')
        trace_id = kwargs.get('trace_id') or f"tc-{_uuid.uuid4().hex[:12]}"

        # 提取上下文参数
        context = {
            'agent_id': agent_id,
            'session_id': kwargs.get('session_id'),
            'llm_client': kwargs.get('llm_client'),
            'trace_id': trace_id,
        }

        # ── 别名归一化 / strict 校验（入口处做一次）───────────────────────────
        # 必须在 Guardian / read-before-edit gate / 写前备份 / read 缓存之前：
        # 这些层都按参数名取路径，看到的必须是 canonical key。放在 _dispatch 里做的
        # 后果是每一层各自维护一张"这个工具的路径键叫什么"的表，然后漂移——
        # backup 把 edit 的键写成 file_path 就导致 edit 从来没触发过写前备份。
        # _dispatch 里仍会再校验一次（对已归一化的 args 是幂等的），保证直调 _dispatch
        # 的路径也有 strict 门。
        tool_meta = self.tools.get(name)
        if tool_meta is not None and isinstance(arguments, dict):
            try:
                arguments = _normalize_aliases_and_validate(
                    name, arguments,
                    tool_meta.get("input_schema"), tool_meta.get("arg_aliases"),
                )
            except InputValidationError as e:
                error_msg = str(e)
                self._record_stats(
                    name, False, json_repair_layers,
                    error_type="invalid_input",
                    error_message=error_msg,
                    execution_time_ms=(time.time() - start_time) * 1000,
                    agent_id=agent_id,
                    session_id=context.get('session_id'),
                    trace_id=trace_id,
                )
                return f"Error: {error_msg}"

        # Guardian 拦截（只在有 agent_id 时启用；DENY 即拒，CONFIRM 当前暂按 ALLOW 处理）
        if agent_id:
            try:
                from backend.guardian import get_guardian
                from backend.tools.defense.guardian import Action as _GuardAction
                action, reason = get_guardian().intercept(agent_id, name, arguments)
                if action == _GuardAction.DENY:
                    execution_time_ms = (time.time() - start_time) * 1000
                    self._record_stats(
                        name, False, json_repair_layers,
                        error_type="guardian_denied",
                        error_message=reason,
                        execution_time_ms=execution_time_ms,
                        agent_id=agent_id,
                        session_id=context.get('session_id'),
                        trace_id=trace_id,
                    )
                    return f"Error: Guardian denied: {reason or '权限不足'}"
            except Exception as e:
                # Guardian 本身故障不应阻断正常调用；记日志后继续
                logger.warning(f"Guardian intercept failed, fallthrough: {e}")

        # read-before-edit gate：修改"已存在文件"的工具必须本会话内 read 过该文件且 mtime 未变
        # 没 session_id 的调用（脚本/后台）跳过；新建文件场景（path 不存在）也跳过
        if name in _GATED_WRITE_TOOLS and context.get('session_id'):
            gate_result = _check_read_before_edit_gate(name, arguments, context.get('session_id'))
            if gate_result is not None:
                execution_time_ms = (time.time() - start_time) * 1000
                self._record_stats(
                    name, False, json_repair_layers,
                    error_type="read_before_edit_violation",
                    error_message=gate_result,
                    execution_time_ms=execution_time_ms,
                    agent_id=agent_id,
                    session_id=context.get('session_id'),
                    trace_id=trace_id,
                )
                return f"Error: {gate_result}"

        # 写前备份（自进化护网：Guardian 通过后、实际写盘前；备份失败不阻断写）
        backup_path = None
        try:
            from backend.tools.defense.backup import WRITE_TOOLS as _WRITE_TOOLS
        except Exception:
            _WRITE_TOOLS = frozenset()
        is_write_tool = name in _WRITE_TOOLS
        if is_write_tool:
            try:
                from backend.tools.defense.backup import backup_before_write
                backup_path = backup_before_write(name, arguments, agent_id)
            except Exception as e:
                logger.warning(f"backup_before_write hook failed: {e}")

        try:
            # 走 _dispatch，成功=走到底；失败=抛异常（不再靠结果字符串前缀判定）
            result = await self._dispatch(name, arguments, context=context)

            # Guardian 输出脱敏：仅对可能暴露跨角色目录结构的工具过
            if agent_id and name in ("grep", "list_directory", "grep_content"):
                try:
                    from backend.guardian import get_guardian
                    result = get_guardian().sanitize_output(agent_id, name, result)
                except Exception as e:
                    logger.warning(f"Guardian sanitize_output failed: {e}")

            # ToolResultBudget 截断。豁免名单：返回结构化协议载荷的工具
            # 必须保证 JSON 完整，否则前端无法识别形态（例如 ask_user_question
            # 的 awaiting_user_choice payload 若被截，前端 JSON.parse 失败，
            # 卡片渲染退化成普通工具结果折叠条）。
            # P1#1：拿到原始长度 + 截断标记传给 stats，让可观测层能看到"哪些工具调用超预算"。
            # Stage 1b 修补：含 `_content_blocks` 标记的 result（read_file 图片路径产出，
            # 内含 base64）也豁免——50000 字符默认 budget 会把图 JSON 拦腰截断变成无效
            # JSON，`_extract_content_blocks_from_tool_result` json.loads 失败 → 走字符串
            # fallback → base64 字符串前缀直接泄漏给 OpenAI 兼容模型（deepseek 等），
            # 它们读 PNG header 推宽高色调后**自信瞎编**画面内容。事故复现于 2026-05-11。
            result_size_chars = len(result) if isinstance(result, str) else None
            budget_truncated = False
            is_content_blocks_payload = (
                isinstance(result, str)
                and result.lstrip().startswith("{")
                and "_content_blocks" in result
            )
            if name not in _BUDGET_EXEMPT_TOOLS and not is_content_blocks_payload:
                try:
                    result, _budget_meta = _BUDGET.apply_with_meta(result)
                    if _budget_meta is not None:
                        budget_truncated = True
                except Exception as e:
                    logger.warning(f"Budget apply failed: {e}")

            execution_time_ms = (time.time() - start_time) * 1000
            self._record_stats(
                name, True, json_repair_layers,
                error_type=None,
                error_message=None,
                execution_time_ms=execution_time_ms,
                agent_id=agent_id,
                session_id=context.get('session_id'),
                trace_id=trace_id,
                result_size_chars=result_size_chars,
                budget_truncated=budget_truncated,
            )

            # read-before-edit gate 配套：read 后填缓存、write 后刷新 mtime
            if context.get('session_id'):
                try:
                    _update_read_cache_after_success(name, arguments, result, context.get('session_id'))
                except Exception as e:
                    logger.warning(f"session_read_cache update failed: {e}")

            # 写后审计（自进化护网：JSONL 落盘 + SSE 通知）
            if is_write_tool:
                try:
                    from backend.tools.defense.backup import (
                        record_backend_write,
                        emit_backend_modified_sse,
                    )
                    record_backend_write(
                        name, arguments, agent_id,
                        context.get("session_id"), backup_path, success=True,
                    )
                    await emit_backend_modified_sse(
                        name, arguments, agent_id,
                        context.get("session_id"), backup_path,
                    )
                except Exception as e:
                    logger.warning(f"backend write audit hook failed: {e}")

            return result

        except KeyError as e:
            # 工具不存在（来自 _dispatch）
            execution_time_ms = (time.time() - start_time) * 1000
            error_msg = str(e).strip("'\"")
            self._record_stats(
                name, False, json_repair_layers,
                error_type="tool_not_found",
                error_message=error_msg,
                execution_time_ms=execution_time_ms,
                agent_id=agent_id,
                session_id=context.get('session_id'),
                trace_id=trace_id,
            )
            return f"Error: {error_msg}"

        except InputValidationError as e:
            # 参数不合 schema（未知字段）——是模型侧错误，不是系统故障。
            # 单独走一个 stats error_type 便于后续看“哪个工具被误用最多”。
            execution_time_ms = (time.time() - start_time) * 1000
            error_msg = str(e)
            self._record_stats(
                name, False, json_repair_layers,
                error_type="invalid_input",
                error_message=error_msg,
                execution_time_ms=execution_time_ms,
                agent_id=agent_id,
                session_id=context.get('session_id'),
                trace_id=trace_id,
            )
            return f"Error: {error_msg}"

        except Exception as e:
            # handler 执行异常
            logger.exception(f"Error executing tool {name}")
            execution_time_ms = (time.time() - start_time) * 1000
            error_msg = str(e)
            self._record_stats(
                name, False, json_repair_layers,
                error_type=type(e).__name__,
                error_message=error_msg,
                execution_time_ms=execution_time_ms,
                agent_id=agent_id,
                session_id=context.get('session_id'),
                trace_id=trace_id,
            )
            return f"Error: {error_msg}"

    def _record_stats(
        self,
        tool_name: str,
        success: bool,
        json_repair_layers,
        error_type,
        error_message,
        execution_time_ms: float,
        agent_id: Optional[str] = None,
        session_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        result_size_chars: Optional[int] = None,
        budget_truncated: Optional[bool] = None,
    ):
        """写入工具调用统计；自身故障不应影响主路径。"""
        try:
            # 必须用 backend.tool_stats（与项目其他模块的导入路径一致），不是顶层
            # 'tool_stats' —— 后者会创建独立的 ToolStatsCollector 单例，导致同进程内
            # 出现两套 collector，写一边读一边数据错位。这条 import 路径修复了这个分裂。
            from backend.tool_stats import get_stats_collector
            stats = get_stats_collector()
            stats.record_call(
                tool_name=tool_name,
                success=success,
                retry_count=0,
                json_repair_layers=json_repair_layers,
                error_type=error_type,
                error_message=error_message,
                execution_time_ms=execution_time_ms,
                agent_id=agent_id,
                session_id=session_id,
                trace_id=trace_id,
                result_size_chars=result_size_chars,
                budget_truncated=budget_truncated,
            )
        except Exception as e:
            logger.warning(f"Failed to record stats: {e}")


# 全局 Budget 单例（模块加载时从环境变量读一次，与 ToolLoopGuard.from_env 行为一致）
from backend.tools.defense.budget import ToolResultBudget as _ToolResultBudget
_BUDGET = _ToolResultBudget.from_env()

# Budget 豁免名单：返回结构化协议载荷的工具（非自由文本），截断会破坏前端形态识别
_BUDGET_EXEMPT_TOOLS = frozenset({
    "ask_user_question",  # awaiting_user_choice JSON 必须完整，前端据此渲染选项卡片
    "delegate_acp_agent",  # output/delegate_id JSON 必须完整，前端 AcpDelegateCard 合成 chunks 依赖此
    "fast_context_search",  # 完整 JSON 结构（rounds, results, _meta）用于前端 FastContextCard 回放
    # file_diff sentinel（_runtime_block: file_diff）—— FileDiffCard 依赖完整 hunks JSON
    "edit",
    "edit_file",
    "multi_edit",
    "write_file",
    # read_file 结构化 JSON —— ReadFileCard 依赖完整 content
    "read_file",
})

# ============================================================================
# read-before-edit gate
# ============================================================================
#
# 受 gate 保护的写工具：调用前要求"本会话内 read 过该文件且 mtime 未变"。
# 防止模型凭幻觉旧文本执行 patch 失败 → 重试 → 再失败的 streak_fail 模式。
#
# 例外：
# - 没 session_id（脚本/后台调用）跳过 gate
# - 文件不存在（新建场景）跳过 gate（write_file 创建新文件被允许）
# - notebook_edit 用 cell_number 定位、不依赖文本匹配，gate 暂不强制
_GATED_WRITE_TOOLS = frozenset({"edit", "multi_edit", "edit_file", "write_file"})


def _check_read_before_edit_gate(
    name: str, arguments: dict, session_id: str
):
    """返回 None=放行，str=拒绝原因。"""
    path = arguments.get("path")
    if not path:
        return None  # 没 path 字段的工具不强制（实际上 gated 工具都有 path）
    try:
        from backend.tools.session_read_cache import get_read_cache
        cache = get_read_cache()
        allowed, reason = cache.check_read_before_edit(session_id, path)
        if allowed:
            return None
        return reason
    except Exception as exc:
        # gate 自身故障不阻断主路径
        logger.warning(f"read_before_edit gate self-error, fallthrough: {exc}")
        return None


def _update_read_cache_after_success(
    name: str, arguments: dict, result: str, session_id: str
) -> None:
    """读工具成功 → 记录；写工具成功 → 刷新 mtime（让后续编辑可继续）。"""
    if not session_id:
        return
    try:
        from backend.tools.session_read_cache import get_read_cache
        cache = get_read_cache()
    except Exception:
        return

    if name == "read_file":
        # 只记单文件 path（paths 字段 2026-05-12 A 方案后已删除）。
        # 模型要读多文件靠并行 read_file 调用，每个调用独立入缓存。
        path = arguments.get("path")
        if path:
            cache.record_read(session_id, path)
        try:
            parsed = json.loads(result)
            if isinstance(parsed, dict) and parsed.get("path") and parsed.get("path") != path:
                cache.record_read(session_id, parsed["path"])
        except Exception:
            pass
    elif name in _GATED_WRITE_TOOLS:
        # 写后刷新 mtime（让同 session 的后续 patch 能继续，否则会报 "file changed since last read"）
        path = arguments.get("path")
        if path:
            cache.invalidate(session_id, path)
        try:
            parsed = json.loads(result)
            if isinstance(parsed, dict) and parsed.get("path") and parsed.get("path") != path:
                cache.invalidate(session_id, parsed["path"])
        except Exception:
            pass


def format_tool_result_for_display(name: str, result: str) -> str:
    """SSE 推送给前端的 tool_result 文本。

    对 _BUDGET_EXEMPT_TOOLS 名单内的工具（结构化协议载荷）发完整 result，
    避免前端 JSON.parse 失败而无法识别形态；其他工具截到 500 字符 + 省略号
    防止过长文字溢出展示。

    与 _BUDGET.apply 的关系：
    - _BUDGET 决定给"上游 LLM"看的版本（execute 内部）
    - 本函数决定给"前端 SSE"看的版本（llm_client / anthropic_client 流式产出）
    豁免同名单是为了让两条路径对结构化工具的处理一致。
    """
    if name in _BUDGET_EXEMPT_TOOLS:
        return result
    return result[:500] + ("..." if len(result) > 500 else "")
