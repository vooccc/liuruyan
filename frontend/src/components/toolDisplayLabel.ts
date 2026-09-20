/**
 * 工具卡 header 摘要句生成器（2026-05-16）。
 *
 * 设计参考：OpenHands `expandable-message.tsx` 的 i18n template UX——
 * 不是单纯显示 "read_file"，而是 "Reading <path>backend/x.py</path>"
 * 这种人能扫读的句子，让用户不展开 args 也知道这步在做什么。
 *
 * 柳如烟的折中：保留工具名（开发者识别工具用） + 追加核心参数摘要句。
 * UI 渲染成 `{toolName} · {summary}`。识别不出参数则只显示工具名。
 *
 * 高频工具单独定制 case 分支；冷工具走 generic fallback（找
 * path/query/command/url/title 任一字段）。
 */

const MAX_SUMMARY_LEN = 80

function trim(s: string, max = MAX_SUMMARY_LEN): string {
  if (!s) return ''
  return s.length > max ? `${s.slice(0, max)}…` : s
}

function asString(v: unknown): string {
  if (v == null) return ''
  if (typeof v === 'string') return v
  try { return String(v) } catch { return '' }
}

function asArray<T>(v: unknown): T[] {
  return Array.isArray(v) ? (v as T[]) : []
}

/**
 * 路径短化：长路径折叠成 basename，避免 header 被一行十层目录撑爆。
 * 阈值 36 字符——既能容纳 `frontend/src/components/x.tsx` 这种常见相对路径
 * 完整可见，又能把 `./backend\tools\builtin\xxx.py`
 * 这种长绝对路径折成 `xxx.py`。返回值仅用于显示，不影响数据流。
 */
function shortPath(path: string, maxLen = 36): string {
  if (!path) return ''
  if (path.length <= maxLen) return path
  // 同时支持 / 和 \（Windows 绝对路径）
  const m = path.match(/[^/\\]+$/)
  return m ? m[0] : path
}

export function getToolDisplaySummary(
  toolName: string | undefined,
  args: Record<string, any> | undefined | null,
): string | undefined {
  if (!toolName || !args || typeof args !== 'object') return undefined

  switch (toolName) {
    case 'read_file': {
      const path = asString(args.path || args.file_path)
      if (!path) return undefined
      // backend schema：offset (1-indexed start) + limit (line count)，end = offset + limit - 1
      const offset = typeof args.offset === 'number' ? args.offset : Number(args.offset) || undefined
      const limit = typeof args.limit === 'number' ? args.limit : Number(args.limit) || undefined
      let range = ''
      if (offset && limit) range = `:${offset}-${offset + limit - 1}`
      else if (offset) range = `:${offset}`
      return trim(shortPath(path) + range)
    }
    case 'write_file':
    case 'edit':
    case 'multi_edit':
    case 'notebook_edit':
    case 'check_typescript':
    case 'get_diagnostics':
    case 'ide_get_diagnostics':
    case 'ide_open_file':
    case 'archive_lry_file':
    case 'restore_lry_file': {
      const path = asString(args.path || args.file_path || args.target_file || args.absolute_path)
      return path ? trim(shortPath(path)) : undefined
    }
    // outline_file 用 `paths` 数组（不是单数 path）；批量调用时显 "x.py 等 N 个"
    case 'outline_file': {
      const paths = asArray<string>(args.paths)
      if (paths.length === 0) {
        // 兜底：模型偶尔会传单数 path（虽然 schema 不允许）
        const fallback = asString(args.path || args.file_path)
        return fallback ? trim(shortPath(fallback)) : undefined
      }
      const first = shortPath(paths[0])
      return paths.length > 1 ? trim(`${first} 等 ${paths.length} 个`) : trim(first)
    }
    case 'fast_context_search': {
      const q = asString(args.query || args.pattern)
      return q ? trim(`"${q}"`) : undefined
    }
    case 'grep':
    case 'glob_search': {
      const pattern = asString(args.pattern || args.query || args.regex)
      const path = asString(args.path || args.directory)
      const short = path ? shortPath(path) : ''
      if (pattern && short) return trim(`"${pattern}" in ${short}`)
      if (pattern) return trim(`"${pattern}"`)
      if (short) return trim(short)
      return undefined
    }
    case 'execute_command':
    case 'ide_run_command_in_terminal': {
      const cmd = asString(args.command || args.cmd || args.CommandLine)
      if (!cmd) return undefined
      return trim(`\`${cmd}\``)
    }
    case 'command_status': {
      return trim(asString(args.command_id || args.id || args.CommandId))
    }
    case 'web_search': {
      const q = asString(args.query)
      if (!q) return undefined
      return trim(`"${q}"`)
    }
    case 'web_extract': {
      const urls = asArray<string>(args.urls)
      if (urls.length > 1) return trim(`${urls[0]} 等 ${urls.length} 个`)
      return trim(asString(args.url || urls[0]))
    }
    case 'ask_user_question': {
      const qs = asArray<{ question?: string }>(args.questions)
      const first = qs[0]?.question || asString(args.question)
      if (!first) return undefined
      return trim(`"${first}"`)
    }
    case 'browser_preview': {
      return trim(asString(args.url || args.Url))
    }
    case 'spawn_subagent': {
      const task = asString(args.task || args.prompt || args.description || args.Prompt)
      const agent = asString(args.agent_id || args.role || args.AgentId)
      if (task && agent) return trim(`${agent}: ${task}`)
      if (task) return trim(task)
      if (agent) return trim(agent)
      return undefined
    }
    case 'recall_memory':
    case 'recall_project_memory':
    case 'session_search': {
      const q = asString(args.query || args.Query)
      if (!q) return undefined
      return trim(`"${q}"`)
    }
    case 'save_project_memory':
    case 'create_memory': {
      return trim(asString(args.title || args.Title || args.content || args.Content))
    }
    case 'add_project_rule':
    case 'add_project_workflow':
    case 'get_project_workflow':
    case 'list_project_rules':
    case 'update_project_notes': {
      return trim(asString(args.title || args.name || args.path))
    }
    case 'task_create':
    case 'task_update':
    case 'task_get':
    case 'task_list': {
      return trim(asString(args.title || args.task_id || args.id))
    }
    case 'git_blame':
    case 'git_diff':
    case 'git_log':
    case 'git_status': {
      const v = asString(args.path || args.file || args.ref || '')
      return v ? trim(shortPath(v)) : undefined
    }
    case 'cron_create':
    case 'cron_delete':
    case 'cron_list':
    case 'cron_set_enabled': {
      return trim(asString(args.name || args.id || args.title))
    }
    case 'code_intelligence': {
      return trim(asString(args.symbol || args.query || args.path))
    }
    case 'read_logs':
    case 'read_frontend_events':
    case 'memory_audit':
    case 'tool_self_reflect':
    case 'sleep':
    case 'python':
    case 'typescript': {
      // 这些工具参数对人类摘要价值不大，留空
      return undefined
    }
    default: {
      // 通用 fallback：扫几个最常见的"可读字段"。前 4 个是路径类，走 shortPath；
      // 后面是 query/command 等非路径字段，原样显示。
      const pathKeys = ['path', 'file_path', 'target_file', 'absolute_path']
      for (const key of pathKeys) {
        const v = (args as Record<string, unknown>)[key]
        if (typeof v === 'string' && v) return trim(shortPath(v))
      }
      for (const key of ['query', 'command', 'url', 'title', 'name', 'pattern']) {
        const v = (args as Record<string, unknown>)[key]
        if (typeof v === 'string' && v) return trim(v)
      }
      return undefined
    }
  }
}
