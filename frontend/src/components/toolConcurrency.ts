/**
 * 工具并发类 mirror 表（前端 UI 分组用）。
 *
 * ⚠️ 单一真相源在后端：`backend/tools/builtin/clean_registry.py` 的
 * `_DEFAULT_CONCURRENCY_CLASSES`（约 line 300-347）。前端这份是镜像，仅用于
 * UI 层把同一轮 LLM 输出的工具按"并行段 / 串行段"渲染（让用户看出哪些工具
 * 在并发执行、哪些在等前面完成），不影响实际执行调度。
 *
 * 同步规则：
 *   1. 后端新增/改动 `_DEFAULT_CONCURRENCY_CLASSES` 时，本文件同步更新
 *   2. 表里没有的工具按 `unknown` 处理（fallback 见 `getConcurrencyClass`）
 *   3. MCP 工具（`mcp__server__name`）后端默认 read，本表 fallback 也按 read
 *
 * fallback 选 read 而非 exclusive 的理由：
 *   - 错认成 read：UI 显示"和别人并行"，最坏情况是视觉略乐观（实际后端按
 *     exclusive 串行跑）——但用户仍能看到 tool 一个个完成，不会误解系统行为
 *   - 错认成 exclusive：每个未知工具被切成单独段 → 分组失去意义、视觉破碎
 *   - 大部分新加工具是 read 类（mcp / 查询类居多），按概率 read 命中率更高
 */

export type ConcurrencyClass = 'read' | 'write' | 'exclusive'

/**
 * 完整 mirror，按 backend `_DEFAULT_CONCURRENCY_CLASSES` 顺序排列以便对比。
 * 注释里的语义说明也对齐后端，改动时两边一起改。
 */
const TOOL_CONCURRENCY_MAP: Record<string, ConcurrencyClass> = {
  // 读类：文件 / 搜索 / 诊断 / 历史 / 网络 / 内存查询 / 任务查询 / 反思
  read_file: 'read',
  grep: 'read',
  glob_search: 'read',
  outline_file: 'read',
  git_status: 'read',
  git_log: 'read',
  git_diff: 'read',
  git_blame: 'read',
  get_diagnostics: 'read',
  check_typescript: 'read',
  code_intelligence: 'read',
  web_search: 'read',
  web_extract: 'read',
  browser_preview: 'read',
  read_logs: 'read',
  read_frontend_events: 'read',
  session_search: 'read',
  recall_memory: 'read',
  task_get: 'read',
  task_list: 'read',
  tool_self_reflect: 'read',
  command_status: 'read',
  cron_list: 'read',
  // 写类：磁盘 / 持久状态突变
  write_file: 'write',
  edit: 'write',
  multi_edit: 'write',
  notebook_edit: 'write',
  create_memory: 'write',
  memory_audit: 'write',
  task_create: 'write',
  task_update: 'write',
  cron_create: 'write',
  cron_delete: 'write',
  cron_set_enabled: 'write',
  // 独占：长阻塞 / 与外部强交互
  execute_command: 'exclusive',
  execute_powershell: 'exclusive',
  ask_user_question: 'exclusive',
  spawn_subagent: 'exclusive',
  sleep: 'exclusive',
}

/**
 * 查工具并发类。MCP 工具（`mcp__*` 前缀）后端默认 read（见 mcp_tool_bridge），
 * 这里也按 read 处理；其他未知工具按 read fallback（理由见文件头部注释）。
 */
export function getConcurrencyClass(toolName: string | undefined): ConcurrencyClass {
  if (!toolName) return 'read'
  const known = TOOL_CONCURRENCY_MAP[toolName]
  if (known) return known
  // MCP 桥接工具：mcp__server__toolname 默认 read
  if (toolName.startsWith('mcp__')) return 'read'
  return 'read'
}

/**
 * 给定一组工具名（按 LLM 输出顺序），按后端 dispatch_batch 算法分段：
 *   - 连续 read → 'parallel' 段（gather 并发执行）
 *   - 单个 write/exclusive → 'sequential' 段（独立 await）
 *
 * 严格对齐 `backend/tools/builtin/clean_registry.py:557-567`。
 *
 * 返回的每个段的 `indices` 是入参数组的下标；调用方用这个下标去 segments 里
 * 取真正的 tool_call segment。
 */
export interface ToolStage {
  type: 'parallel' | 'sequential'
  klass: ConcurrencyClass    // parallel 段一定是 read；sequential 段是 write 或 exclusive
  indices: number[]          // 入参 toolNames 数组的下标
}

export function buildToolStages(toolNames: (string | undefined)[]): ToolStage[] {
  const stages: ToolStage[] = []
  let pendingReads: number[] = []

  const flushReads = () => {
    if (pendingReads.length === 0) return
    stages.push({
      type: 'parallel',
      klass: 'read',
      indices: pendingReads,
    })
    pendingReads = []
  }

  for (let i = 0; i < toolNames.length; i++) {
    const klass = getConcurrencyClass(toolNames[i])
    if (klass === 'read') {
      pendingReads.push(i)
    } else {
      flushReads()
      stages.push({
        type: 'sequential',
        klass,
        indices: [i],
      })
    }
  }
  flushReads()

  return stages
}
