// Terminal 生命周期管理 —— 给 ide_open_terminal / ide_run_command_in_terminal 用。
//
// 职责：
//   - 按 terminal_id (uuid) 索引 vscode.Terminal 实例
//   - 命名复用：reuse=true 且存在同名 terminal → 复用同一个
//   - onDidCloseTerminal 时清理 map，避免用户手动关了 terminal 后 id 悬挂
//
// Shell integration：
//   - terminal.shellIntegration 可能为 undefined（VSCode 版本低 / shell 不支持）
//   - 订阅 window.onDidChangeTerminalShellIntegration 事件可以等它激活
//   - 调 executeCommand 前最多等 2s，超时降级 fire-and-forget sendText
//
// 这里不做实际 sendText / executeCommand，只管 terminal lifecycle；调用方在 ide-routes.ts。

import * as vscode from 'vscode'
import { randomUUID } from 'crypto'
import { log } from './utils/logger'

interface TerminalEntry {
  id: string
  terminal: vscode.Terminal
  createdAt: number
}

const _terminals = new Map<string, TerminalEntry>()

/**
 * 初始化 terminal lifecycle 订阅。
 * 由 extension.ts activate 调用并把 disposable push 到 context.subscriptions。
 */
export function initTerminals(context: vscode.ExtensionContext): void {
  context.subscriptions.push(
    vscode.window.onDidCloseTerminal((closed) => {
      for (const [id, entry] of _terminals.entries()) {
        if (entry.terminal === closed) {
          _terminals.delete(id)
          log(`[terminals] cleaned up closed terminal id=${id}`)
          break
        }
      }
    }),
  )
}

/**
 * 创建或复用一个 terminal。
 *
 * @param name     terminal 显示名，默认 "柳如烟"
 * @param cwd      工作目录（绝对路径），不传则沿用 VSCode 默认
 * @param reuse    true（默认）= 如果已有同名 entry 返回现有；false = 总是新建
 * @returns        { id, terminal, created }
 */
export function openOrReuseTerminal(opts: {
  name?: string
  cwd?: string
  reuse?: boolean
}): { id: string; terminal: vscode.Terminal; created: boolean; name: string } {
  const name = opts.name && opts.name.trim() ? opts.name.trim() : '柳如烟'
  const reuse = opts.reuse !== false  // 默认 true

  if (reuse) {
    for (const entry of _terminals.values()) {
      if (entry.terminal.name === name) {
        // 安全检查：某些极端情况下 terminal 被用户关了但事件还没来，
        // 需要用 terminal.exitStatus 判断（已退出）
        if (entry.terminal.exitStatus === undefined) {
          return { id: entry.id, terminal: entry.terminal, created: false, name }
        }
      }
    }
  }

  const terminal = vscode.window.createTerminal({
    name,
    cwd: opts.cwd,
  })
  const id = randomUUID().replace(/-/g, '')
  _terminals.set(id, { id, terminal, createdAt: Date.now() })
  log(`[terminals] created new terminal id=${id} name=${name}${opts.cwd ? ` cwd=${opts.cwd}` : ''}`)
  return { id, terminal, created: true, name }
}

/**
 * 根据 id 找 terminal。找不到 / 已关闭 返回 null。
 */
export function getTerminal(id: string): vscode.Terminal | null {
  const entry = _terminals.get(id)
  if (!entry) return null
  if (entry.terminal.exitStatus !== undefined) {
    _terminals.delete(id)
    return null
  }
  return entry.terminal
}

/**
 * 尝试用 shell integration 执行命令并收集 output。
 *
 * 策略：
 *   1. 如果 shellIntegration 已可用 → 直接 executeCommand + read stream
 *   2. 如果不可用 → 订阅 onDidChangeTerminalShellIntegration 等最多 waitMs ms
 *   3. 到点还不可用 → 返回 null，让调用方降级 fire-and-forget
 *
 * @param terminal  目标 terminal
 * @param command   要执行的命令
 * @param waitMs    等 shell integration 激活的最长毫秒数（默认 2000）
 * @param readTimeoutMs  读 output 的最长时间（默认 60000，防止长 build 卡死 bridge）
 */
export async function executeWithShellIntegration(
  terminal: vscode.Terminal,
  command: string,
  waitMs: number = 2000,
  readTimeoutMs: number = 60000,
): Promise<{ output: string; exitCode: number | null; captured: true } | null> {
  terminal.show(true)  // preserveFocus=true 不抢 webview 焦点

  // 等 shell integration 激活
  let shellIntegration: vscode.TerminalShellIntegration | undefined = terminal.shellIntegration
  if (!shellIntegration) {
    shellIntegration = await new Promise<vscode.TerminalShellIntegration | undefined>((resolve) => {
      const timeoutHandle = setTimeout(() => {
        sub.dispose()
        resolve(undefined)
      }, waitMs)
      const sub = vscode.window.onDidChangeTerminalShellIntegration((e) => {
        if (e.terminal === terminal) {
          clearTimeout(timeoutHandle)
          sub.dispose()
          resolve(e.shellIntegration)
        }
      })
    })
  }

  if (!shellIntegration) {
    log(`[terminals] shell integration unavailable for terminal "${terminal.name}"`)
    return null
  }

  // 用 shell integration 执行
  const execution = shellIntegration.executeCommand(command)
  let output = ''
  let exitCode: number | null = null

  // 限时读 stream
  const readPromise = (async () => {
    try {
      for await (const chunk of execution.read()) {
        output += chunk
      }
    } catch (err) {
      log(`[terminals] read stream error: ${err instanceof Error ? err.message : String(err)}`)
    }
  })()

  // 限时等命令结束
  const endPromise = new Promise<void>((resolve) => {
    const sub = vscode.window.onDidEndTerminalShellExecution((e) => {
      if (e.execution === execution) {
        exitCode = e.exitCode ?? null
        sub.dispose()
        resolve()
      }
    })
  })

  const timeoutPromise = new Promise<void>((resolve) =>
    setTimeout(resolve, readTimeoutMs),
  )

  await Promise.race([endPromise, timeoutPromise])
  // 额外给 read stream 一点时间把尾部 chunks 处理完（不能无限等）
  await Promise.race([readPromise, new Promise((r) => setTimeout(r, 300))])

  return { output, exitCode, captured: true }
}

/**
 * 观测辅助：当前管理的 terminal 总数（给 /health 用，不对外）
 */
export function terminalStats(): { count: number; entries: { id: string; name: string; age_ms: number }[] } {
  const now = Date.now()
  return {
    count: _terminals.size,
    entries: Array.from(_terminals.values()).map((e) => ({
      id: e.id,
      name: e.terminal.name,
      age_ms: now - e.createdAt,
    })),
  }
}
